import dataclasses
from dataclasses import dataclass
from typing import Dict, Optional, Type, Union

import equinox as eqx
import jax.random as jrandom

import haliax as hax
import haliax.nn as hnn
from haliax import Axis, NamedArray
from haliax.jax_utils import maybe_rng_split

from levanter.compat.hf_checkpoints import HFCheckpointConverter
from levanter.compat.torch_serialization import (
    StateDict,
    StateDictSerializationMixin,
    apply_prefix,
    flatten_linear_layers,
    unflatten_linear_layers,
)
from levanter.logging import silence_transformer_nag
from levanter.models.attention import AttentionMask
from levanter.models.llama import LlamaConfig, LlamaEmbedding, LlamaTransformer
from levanter.models.lm_model import LmConfig, LmHeadModel
from levanter.utils.py_utils import cached_classproperty


silence_transformer_nag()
from transformers import MistralConfig as HfMistralConfig  # noqa: E402
from transformers import PretrainedConfig as HfConfig  # noqa: E402


@LmConfig.register_subclass("mistral")
@dataclass(frozen=True)
class MistralConfig(LlamaConfig):
    """Config for MistralModel

    Args:
        seq_len (int, optional): maximum length of the input sequence. Defaults to 8192.
        hidden_dim (int, optional): dimension of the hidden state. Defaults to 4096.
        intermediate_dim (int, optional): dimension of the intermediate state. Defaults to 14336.
        num_layers (int, optional): number of hidden layers in the Transformer encoder. Defaults to 32.
        num_heads (int, optional): number of attention heads for each attention layer. Defaults to 32.
        num_kv_heads (int, optional): number of attention heads for keys and values in each attention layer.
            Setting to 1 means MQA. Setting to num_heads means MHA. Otherwise GQA.
            Note that num_heads must be divisible by this number. Defaults to 8.
        activation_function (str, optional): activation function for the hidden layer. Defaults to "silu".
        sliding_window (int, optional): window size of sliding window attention. Defaults to 4096.
    """

    seq_len: int = 8192
    hidden_dim: int = 4096
    intermediate_dim: int = 14336
    num_layers: int = 32
    num_heads: int = 32
    num_kv_heads: int = 8
    activation_function: str = "silu"
    initializer_range: float = 0.02
    layer_norm_epsilon: float = 1e-6
    sliding_window: int = 4096

    # Attention-related config
    upcast_attn: bool = False
    use_flash_attention: bool = False
    flash_attention_block_size: Optional[int] = None

    gradient_checkpointing: bool = True
    gradient_checkpointing_block_size: int = 5

    use_bias: bool = False
    rope_scaling: Optional[dict] = None

    # Axis
    Pos = property(lambda self: Axis(name="position", size=self.seq_len))
    KeyPos = property(lambda self: self.Pos.alias("key_position"))
    Embed = property(lambda self: Axis(name="embed", size=self.hidden_dim))
    Heads = property(lambda self: Axis(name="heads", size=self.num_heads))
    KVHeads = property(lambda self: Axis(name="kv_heads", size=self.num_kv_heads))
    Layers = property(lambda self: Axis(name="layers", size=self.num_layers))
    Mlp = property(lambda self: Axis(name="mlp", size=self.intermediate_dim))
    HeadSize = property(lambda self: Axis(name="head_size", size=self.hidden_dim // self.num_heads))

    @cached_classproperty
    def default_hf_checkpoint_converter(cls) -> HFCheckpointConverter["MistralConfig"]:  # type: ignore
        return HFCheckpointConverter(
            cls,  # type: ignore
            "mistralai/Mistral-7B-v0.1",
            trust_remote_code=True,
            tokenizer="mistralai/Mistral-7B-v0.1",
            HfConfigClass=HfMistralConfig,
        )

    @classmethod
    def from_hf_config(cls, hf_config: HfConfig):
        return MistralConfig(
            seq_len=hf_config.max_position_embeddings,  # this might be too big...
            hidden_dim=hf_config.hidden_size,
            intermediate_dim=hf_config.intermediate_size,
            num_layers=hf_config.num_hidden_layers,
            num_heads=hf_config.num_attention_heads,
            num_kv_heads=hf_config.num_key_value_heads,
            activation_function=hf_config.hidden_act,
            initializer_range=hf_config.initializer_range,
            layer_norm_epsilon=hf_config.rms_norm_eps,
            sliding_window=hf_config.sliding_window,
        )

    def to_hf_config(self, vocab_size: int, config_overrides: Optional[Dict] = None) -> HfMistralConfig:
        """Convert to HuggingFace's MistralConfig

        Args:
            vocab_size (int, optional): Vocabulary size of the tokenizer. Defaults to 32000.
            config_overrides (dict, optional): Overrides for the config. Defaults to None.

        Returns:
            HfMistralConfig: HuggingFace's MistralConfig
        """
        if config_overrides is None:
            config_overrides = {}

        return HfMistralConfig(
            max_position_embeddings=self.seq_len,
            hidden_size=self.hidden_dim,
            intermediate_size=self.intermediate_dim,
            num_hidden_layers=self.num_layers,
            num_attention_heads=self.num_heads,
            num_key_value_heads=self.num_kv_heads,
            hidden_act=self.activation_function,
            initializer_range=self.initializer_range,
            rms_norm_eps=self.layer_norm_epsilon,
            sliding_window=self.sliding_window,
            vocab_size=vocab_size,
            **config_overrides,
        )

    @property
    def model_type(cls) -> Type["MistralLMHeadModel"]:
        return MistralLMHeadModel


class MistralLMHeadModel(eqx.Module, LmHeadModel[MistralConfig], StateDictSerializationMixin):
    transformer: LlamaTransformer
    embeddings: LlamaEmbedding
    lm_head: hnn.Linear

    @property
    def config(self):
        return self.transformer.config

    @property
    def vocab_size(self) -> int:
        return self.Vocab.size

    @property
    def Vocab(self) -> Axis:
        return self.embeddings.Vocab

    @classmethod
    def init(cls, Vocab: Axis, config: MistralConfig, *, key) -> "MistralLMHeadModel":
        k_t, k_emb = jrandom.split(key, 2)
        transformer = LlamaTransformer.init(config, key=k_t)
        embeddings = LlamaEmbedding.init(Vocab, config, key=k_emb)
        lm_head = hnn.Linear.init(In=config.Embed, Out=Vocab, key=k_emb, use_bias=False, out_first=True)
        return MistralLMHeadModel(transformer, embeddings, lm_head)

    def __call__(
        self,
        input_ids: NamedArray,
        attn_mask: Optional[Union[NamedArray, AttentionMask]] = None,
        *,
        key=None,
    ) -> NamedArray:
        """
        Args:
            input_ids (NamedArray): [batch, position]
                Indices of input sequence tokens in the vocabulary.
            attn_mask (Union[NamedArray, AttentionMask], optional): [batch, position]
                Mask to avoid performing attention on the padding token indices of the encoder input.
                The attn_mask from training pipeline may be an AttentionMask object instead of NamedArray
        """
        k_t, k_head = maybe_rng_split(key, 2)
        x = self.embeddings.embed(input_ids)
        x = self.transformer(x, attn_mask=attn_mask, key=k_t)
        lm_logits = self.lm_head(x, key=k_head)
        return lm_logits

    def resize_vocab(self, new_size: int, key=None) -> "LmHeadModel[MistralConfig]":
        new_Vocab = self.Vocab.resize(new_size)
        k1, k2 = maybe_rng_split(key, 2)
        new_embeddings = self.embeddings.resize_embeddings(new_size, key=k1)
        new_lm_matrix = hax.tree_util.resize_axis(self.lm_head.weight, self.Vocab, new_size, key=k2)
        new_lm_head = dataclasses.replace(self.lm_head, Out=new_Vocab, weight=new_lm_matrix)

        return dataclasses.replace(self, embeddings=new_embeddings, lm_head=new_lm_head)

    def _state_dict_key_map(self) -> Dict[str, Optional[str]]:
        return {"transformer": "model", "embeddings": None}

    def from_state_dict(self, state_dict: StateDict, prefix: Optional[str] = None):
        # unflatten the linear layers of HF state_dict to match the shape of MistralMlp
        d = state_dict.copy()
        d.update(
            unflatten_linear_layers(
                apply_prefix(prefix, "lm_head"), state_dict, self.lm_head, out_dims_first_in_dict=True
            )
        )
        return super().from_state_dict(d, prefix)

    def update_state_dict(self, state_dict: StateDict, prefix: Optional[str] = None) -> StateDict:
        my_dict: StateDict = {}
        super().update_state_dict(my_dict, prefix=prefix)

        my_dict.update(
            flatten_linear_layers(apply_prefix(prefix, "lm_head"), self.lm_head, out_dims_first_in_dict=True)
        )

        state_dict.update(my_dict)
        return state_dict
