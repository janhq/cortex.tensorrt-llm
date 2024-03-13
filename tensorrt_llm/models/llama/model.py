# SPDX-FileCopyrightText: Copyright (c) 2022-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import tempfile
from pathlib import Path
from typing import Optional

from transformers import AutoConfig, AutoModelForCausalLM

from tensorrt_llm.models.llama.weight import (load_from_awq_llama,
                                              load_from_fp8_llama)

from ... import profiler
from ..._utils import pad_vocab_size
from ...functional import RotaryScalingType, Tensor, recv, send
from ...layers import (MOE, Attention, AttentionMaskType, ColumnLinear,
                       Embedding, GatedMLP, MoeConfig, PositionEmbeddingType,
                       PromptTuningEmbedding, RmsNorm)
from ...lora_manager import LoraConfig
from ...mapping import Mapping
from ...module import Module
from ...plugin import init_all_reduce_helper
from ...quantization import QuantMode
from ...top_model_mixin import TopModelMixin
from ..modeling_utils import (DecoderLayerList, DecoderModelForCausalLM,
                              PretrainedConfig)
from .weight import load_from_hf_llama


class LLaMADecoderLayer(Module):

    def __init__(self, config: PretrainedConfig, layer_idx: int):
        super().__init__()
        self.layer_idx = layer_idx
        self.config = config

        self.input_layernorm = RmsNorm(normalized_shape=config.hidden_size,
                                       eps=config.norm_epsilon,
                                       dtype=config.dtype)

        layers_range = config.mapping.pp_layers(config.num_hidden_layers)
        local_layer_idx = layer_idx - layers_range[0]
        self.attention = Attention(
            local_layer_idx=local_layer_idx,
            hidden_size=config.hidden_size,
            num_attention_heads=config.num_attention_heads,
            num_kv_heads=config.num_key_value_heads,
            max_position_embeddings=config.max_position_embeddings,
            dtype=config.dtype,
            attention_mask_type=AttentionMaskType.causal,
            bias=config.attn_bias,
            position_embedding_type=PositionEmbeddingType.rope_gpt_neox,
            rotary_embedding_base=config.rotary_base,
            rotary_embedding_scaling=config.rotary_scaling,
            tp_group=config.mapping.tp_group,
            tp_size=config.mapping.tp_size,
            tp_rank=config.mapping.tp_rank,
            quant_mode=config.quant_mode,
            max_lora_rank=config.max_lora_rank)

        mlp_hidden_size = config.hidden_size * 4 if config.intermediate_size is None else config.intermediate_size

        ClsMLP = GatedMLP
        mlp_kwargs = {}
        if config.moe_num_experts > 1:
            ClsMLP = MOE
            mlp_kwargs = {
                "moe_config":
                MoeConfig(
                    config.moe_num_experts,
                    config.moe_top_k,
                    config.moe_tp_mode,
                    config.moe_normalization_mode,
                ),
                "tp_rank":
                config.mapping.tp_rank,
            }

        self.mlp = ClsMLP(hidden_size=config.hidden_size,
                          ffn_hidden_size=mlp_hidden_size,
                          hidden_act=config.hidden_act,
                          dtype=config.dtype,
                          bias=config.mlp_bias,
                          tp_group=config.mapping.tp_group,
                          tp_size=config.mapping.tp_size,
                          quant_mode=config.quant_mode,
                          max_lora_rank=config.max_lora_rank,
                          **mlp_kwargs)
        self.post_layernorm = RmsNorm(normalized_shape=config.hidden_size,
                                      eps=config.norm_epsilon,
                                      dtype=config.dtype)

    def forward(
            self,
            hidden_states,
            attention_mask=None,
            medusa_packed_mask=None,  # For Medusa support
            medusa_position_offsets=None,
            use_cache=False,
            kv_cache_params=None,
            attention_params=None,
            lora_layer_params=None):
        residual = hidden_states
        hidden_states = self.input_layernorm(hidden_states)

        attention_output = self.attention(
            hidden_states,
            attention_mask=attention_mask,
            medusa_packed_mask=medusa_packed_mask,  # For Medusa support
            medusa_position_offsets=medusa_position_offsets,
            use_cache=use_cache,
            kv_cache_params=kv_cache_params,
            attention_params=attention_params,
            lora_layer_params=lora_layer_params)

        if use_cache:
            attention_output, presents = attention_output

        hidden_states = residual + attention_output

        residual = hidden_states
        hidden_states = self.post_layernorm(hidden_states)

        hidden_states = self.mlp(hidden_states,
                                 lora_layer_params=lora_layer_params)

        hidden_states = residual + hidden_states
        if use_cache:
            return (hidden_states, presents)
        return hidden_states


class LLaMAModel(Module):

    def __init__(self, config: PretrainedConfig) -> None:
        super().__init__()
        init_all_reduce_helper()

        self.mapping = config.mapping
        self.use_prompt_tuning = config.use_prompt_tuning
        EmbeddingCls = PromptTuningEmbedding if config.use_prompt_tuning else Embedding
        if self.mapping.is_first_pp_rank():
            self.vocab_embedding = EmbeddingCls(
                num_embeddings=config.vocab_size,
                embedding_dim=config.hidden_size,
                dtype=config.dtype,
                tp_size=self.mapping.tp_size
                if config.use_parallel_embedding else 1,
                tp_group=self.mapping.tp_group
                if config.use_parallel_embedding else None,
                sharding_dim=config.embedding_sharding_dim,
                tp_rank=self.mapping.tp_rank,
            )

        self.layers = DecoderLayerList(LLaMADecoderLayer, config)

        if self.mapping.is_last_pp_rank():
            self.ln_f = RmsNorm(normalized_shape=config.hidden_size,
                                eps=config.norm_epsilon,
                                dtype=config.dtype)

    def forward(
            self,
            input_ids,
            position_ids=None,
            use_cache=False,
            attention_mask=None,
            medusa_position_offsets=None,  # For Medusa support
            medusa_packed_mask=None,  # For Medusa support
            kv_cache_params=None,
            attention_params=None,
            hidden_states=None,
            prompt_embedding_table: Optional[Tensor] = None,
            prompt_tasks: Optional[Tensor] = None,
            prompt_vocab_size: Optional[Tensor] = None,
            lora_params=None):

        kv_cache_params.fill_none_tensor_list(len(self.layers))

        if use_cache:
            presents = []

        ptuning_args = [
            prompt_embedding_table, prompt_tasks, prompt_vocab_size
        ] if self.use_prompt_tuning else []

        if self.mapping.is_first_pp_rank():
            hidden_states = self.vocab_embedding(input_ids, *ptuning_args)
        else:
            hidden_states = recv(hidden_states, self.mapping.prev_pp_rank())

        hidden_states = self.layers.forward(
            hidden_states,
            use_cache=use_cache,
            attention_mask=attention_mask,
            kv_cache_params=kv_cache_params,
            attention_params=attention_params,
            lora_params=lora_params,
            medusa_position_offsets=medusa_position_offsets,
            medusa_packed_mask=medusa_packed_mask)

        if use_cache:
            hidden_states, presents = hidden_states

        if self.mapping.is_last_pp_rank():
            hidden_states = self.ln_f(hidden_states)
        else:
            hidden_states = send(hidden_states, self.mapping.next_pp_rank())

        if use_cache:
            return (hidden_states, tuple(presents))
        return hidden_states


class LLaMAForCausalLM(DecoderModelForCausalLM, TopModelMixin):

    def __init__(self, config: PretrainedConfig):
        self.check_config(config)
        transformer = LLaMAModel(config)
        vocab_size_padded = pad_vocab_size(config.vocab_size,
                                           config.mapping.tp_size)
        if config.mapping.is_last_pp_rank():
            lm_head = ColumnLinear(config.hidden_size,
                                   vocab_size_padded,
                                   bias=False,
                                   dtype=config.dtype,
                                   tp_group=config.mapping.tp_group,
                                   tp_size=config.mapping.tp_size,
                                   gather_output=True)
        else:
            lm_head = None
        self.quant_mode = config.quant_mode
        self.mapping = config.mapping
        super().__init__(config, transformer, lm_head)

    def check_config(self, config):
        config.set_if_not_exist('mlp_bias', False)
        config.set_if_not_exist('attn_bias', False)
        config.set_if_not_exist('rotary_base', 10000.0)
        config.set_if_not_exist('rotary_scaling', None)
        config.set_if_not_exist('moe_num_experts', 0)
        config.set_if_not_exist('moe_top_k', 0)
        config.set_if_not_exist('moe_tp_mode',
                                MoeConfig.ParallelismMode.TENSOR_PARALLEL)
        config.set_if_not_exist(
            'moe_normalization_mode',
            MoeConfig.ExpertScaleNormalizationMode.RENORMALIZE)

    @classmethod
    def from_hugging_face(cls,
                          hf_model_dir,
                          dtype='float16',
                          mapping: Optional[Mapping] = None,
                          quant_mode: Optional[QuantMode] = None,
                          **kwargs):
        cfg = AutoConfig.from_pretrained(hf_model_dir)

        num_kv_heads = cfg.num_key_value_heads if hasattr(cfg, "num_key_value_heads") \
            else cfg.num_attention_heads
        use_fused_mlp = kwargs.get("use_fused_mlp", False)
        mapping = mapping or Mapping()
        quant_mode = quant_mode or QuantMode(0)

        cfg.mapping = mapping

        cfg.dtype = dtype
        cfg.quant_mode = quant_mode

        cfg.norm_epsilon = cfg.rms_norm_eps

        if cfg.model_type == 'mixtral':
            moe_config = MoeConfig(
                num_experts=cfg.num_local_experts,
                top_k=cfg.num_experts_per_tok,
                tp_mode=kwargs.get("moe_tp_mode",
                                   MoeConfig.ParallelismMode.TENSOR_PARALLEL),
                normalization_mode=kwargs.get(
                    "moe_normalization_mode",
                    MoeConfig.ExpertScaleNormalizationMode.RENORMALIZE),
            ).validate()
            # HF LLaMA-type models are implicitly using gated activation.
            # With our MoE implementation, we must make it explicit
            cfg.hidden_act = 'swiglu'
            cfg.rotary_base = cfg.rope_theta
        else:
            moe_config = MoeConfig()
        config = {
            'architecture': cfg.architectures[0],
            'dtype': cfg.dtype,
            'logits_dtype': 'float32',
            'num_hidden_layers': cfg.num_hidden_layers,
            'num_attention_heads': cfg.num_attention_heads,
            'hidden_size': cfg.hidden_size,
            'intermediate_size': cfg.intermediate_size,
            'num_key_value_heads': num_kv_heads,
            'vocab_size': cfg.vocab_size,
            'position_embedding_type': 'rope_gpt_neox',
            'max_position_embeddings': cfg.max_position_embeddings,
            'hidden_act': cfg.hidden_act,
            'rotary_base': getattr(cfg, 'rotary_base', 10000.0),
            'rotary_scaling': getattr(cfg, 'rotary_scaling', None),
            'norm_epsilon': cfg.rms_norm_eps,
            'quantization': {
                'group_size': 128,
            },
            "moe_config": {
                "num_experts": moe_config.num_experts,
                "top_k": moe_config.top_k,
                "tp_mode": moe_config.tp_mode,
                "normalization_mode": moe_config.normalization_mode,
            },
            'use_parallel_embedding': kwargs.get("use_parallel_embedding",
                                                 False),
            'embedding_sharding_dim': kwargs.get("embedding_sharding_dim", 0),
            'use_prompt_tuning': kwargs.get("use_prompt_tuning", False),
            'moe_num_experts': moe_config.num_experts,
            'moe_top_k': moe_config.top_k,
            'moe_tp_mode': moe_config.tp_mode,
            'moe_normalization_mode': moe_config.normalization_mode,
            'use_fused_mlp': use_fused_mlp,
        }
        if quant_mode.is_int4_weight_only_per_group():
            config['quantization'].update({
                'quant_algo': 'W4A16_AWQ',
                'has_zero_point': False,
                'pre_quant_scale': True,
                'exclude_modules': [],
            })
        elif quant_mode.has_fp8_qdq() and quant_mode.has_fp8_kv_cache():
            config['quantization'].update({
                'quant_algo': 'FP8',
                'kv_cache_quant_algo': 'FP8'
            })
        else:
            if quant_mode != QuantMode(0):
                raise ValueError(f"Unsupported quantization mode: {quant_mode}")

        model_config = PretrainedConfig.from_dict(config)
        model_config.mapping = mapping
        tllm_llama = LLaMAForCausalLM(model_config)
        q_weights = {}
        if quant_mode.has_any_quant():
            q_weights = tllm_llama._quantize(hf_model_dir, dtype, cfg, **kwargs)

        # For debug purpose, skip weights loading to be faster
        if kwargs.get("skip_loading_weights", False):
            return tllm_llama

        # weights already loaded in _quantize for int4 weight only
        if not quant_mode.is_int4_weight_only_per_group():
            profiler.start("Loading weights from HF")
            hf_llama = AutoModelForCausalLM.from_pretrained(
                hf_model_dir,
                device_map={
                    "model": "cpu",
                    "lm_head": "cpu",
                    "embed_tokens": "cpu",
                    "layers": "cpu",
                    "norm": "cpu",
                },  # Load to CPU memory
                torch_dtype='auto',
            )

            weights = load_from_hf_llama(
                tllm_llama,
                hf_llama,
                mapping=mapping,
                dtype=dtype,
                # TODO: these shall be outside from_hugging_face too.
                use_gemm_woq_plugin=kwargs.get("use_gemm_woq_plugin", False),
                lora_config=kwargs.get("lora_config", LoraConfig()),
            )
            profiler.stop("Loading weights from HF")
            del hf_llama
            weights.update(q_weights)
            tllm_llama.load(weights)
        else:
            tllm_llama.load(q_weights)
        return tllm_llama

    def _quantize(self, hf_model_dir, dtype, cfg, **kwargs):
        '''Given the quant_mode set in the Module object, read from given hf model
           call AMMO to generate quantization scales, and set the scales back the module parameters.
        '''
        # use self destructed temporary path if kwargs[quantization_cache_dir] is not specified
        # sometimes the quantization checkpoint path needs to be saved for debug purpose
        quantized_temp_dir = tempfile.TemporaryDirectory("llama-quantized")
        quantized_checkpoint_path = kwargs.get("quantization_cache_dir",
                                               quantized_temp_dir.name)
        quantize_lm_head = kwargs.get("quantize_lm_head", False)
        quant_mode = cfg.quant_mode
        ammo_qformat = None
        calib_size = None
        if quant_mode.has_fp8_qdq() or quant_mode.has_fp8_kv_cache():
            ammo_qformat = 'fp8'
            calib_size = 512
        # TODO: how to distinguish from quant_mode about int4_awq or int4_gptq?
        elif quant_mode.is_int4_weight_only_per_group():
            ammo_qformat = 'int4_awq'
            calib_size = 32
        assert ammo_qformat is not None

        # local import to avoid pytest issue when importing AMMO and transformers lib
        from .quantize import quantize_llama_and_export
        quantize_llama_and_export(hf_model_dir,
                                  quantized_checkpoint_path,
                                  ammo_qformat,
                                  dtype,
                                  calib_size=calib_size,
                                  quantize_lm_head=quantize_lm_head)

        ckpt = Path(quantized_checkpoint_path) / "llama_tp1_rank0.npz"
        assert ckpt.exists(), f"The expecting checkpoint path {ckpt} does not exist" \
                  "it's likely quantization failed, pls check error logs"
        hf_config = AutoConfig.from_pretrained(hf_model_dir,
                                               trust_remote_code=True)
        if ammo_qformat == 'fp8':
            return load_from_fp8_llama(
                str(ckpt),
                hf_config.num_hidden_layers,
                cfg.mapping,
                fp8_kv_cache=quant_mode.has_fp8_kv_cache())
        else:
            return load_from_awq_llama(str(ckpt),
                                       hf_config.num_hidden_layers,
                                       hf_config.vocab_size,
                                       cfg.mapping,
                                       dtype=dtype)

    # llama specific setters, user shall has the chance to change the module attributes after
    # from_hugging_face factory method created the model when these attributes is not included in the huggingface checkpoint

    def rotary_base(self, val):
        for decoder in self.layers:
            decoder.attention.rotary_embedding_base = val
        return self

    def rotary_scaling(self, scaling_type, factor):
        # TODO: what if there are some other behaviors triggered by the these changes?
        # should implement these assignment as setters of the Attention Module
        assert scaling_type in ("linear", "dynamic"), f"Got {scaling_type}"
        assert factor > 1.0, f"Got {factor}"
        for decoder in self.layers:
            decoder.attention.rotary_embedding_scale_type = RotaryScalingType.linear if scaling_type == "linear" else RotaryScalingType.dynamic
            decoder.attention.rotary_embedding_scale = factor
        return self

    def default_plugin_config(self, **kwargs):
        plugin_config = super().default_plugin_config(**kwargs)
        if self.quant_mode.is_int4_weight_only_per_group():
            plugin_config.set_weight_only_groupwise_quant_matmul_plugin()
        return plugin_config

    @classmethod
    def from_meta_ckpt(cls,
                       meta_ckpt_dir,
                       dtype,
                       mapping,
                       override_fileds=None,
                       **kwargs):
        meta_config = None
        with open(Path(meta_ckpt_dir, "params.json")) as fp:
            meta_config: dict = json.load(fp)
        assert meta_config is not None
        config = {}
        n_embd = meta_config["dim"]
        n_head = meta_config["n_heads"]
        n_layer = meta_config["n_layers"]
        n_kv_head = meta_config.get("n_kv_heads", n_head)
        # meta checkpoint don't have vocab_size|hidden_act|rotary_base specified, need to read from user input
        vocab_size = 32000
        hidden_act = 'silu'
        rotary_base = 10000.0
        if "hidden_dim" in meta_config:
            inter_size = meta_config["hidden_dim"]
        else:
            multiple_of = meta_config.get("multiple_of", 1)
            n_embd_ = int(4 * n_embd * 2 / 3)
            ffn_dim_multiplier = meta_config.get("ffn_dim_multiplier", 1)
            inter_size = multiple_of * (
                (int(n_embd_ * ffn_dim_multiplier) + multiple_of - 1) //
                multiple_of)
        rms_norm_eps = meta_config["norm_eps"]
        moe_num_experts = meta_config.get("moe", {}).get("num_experts", 0)
        moe_top_k = meta_config.get("moe", {}).get("num_experts_per_tok", 0)
        moe_tp_mode = None
        n_positions = 2048
        config['moe_normalization_mode'] = None  # meta checkpoint has no moe
        architecture = "LlamaForCausalLM"
        # config values from reading meta
        config.update({
            'architecture': architecture,
            'dtype': dtype,
            'logits_dtype': 'float32',
            'num_hidden_layers': n_layer,
            'num_attention_heads': n_head,
            'hidden_size': n_embd,
            'intermediate_size': inter_size,
            'num_key_value_heads': n_kv_head,
            'vocab_size': vocab_size,
            'position_embedding_type': 'rope_gpt_neox',
            'max_position_embeddings': n_positions,
            'hidden_act': hidden_act,
            'rotary_base': rotary_base,
            'norm_epsilon': rms_norm_eps,
            'moe_num_experts': moe_num_experts,
            'moe_top_k': moe_top_k,
            'moe_tp_mode': moe_tp_mode,
            'mapping': {
                'world_size': mapping.tp_size * mapping.pp_size,
                'tp_size': mapping.tp_size,
                'pp_size': mapping.pp_size,
            },
        })
        config.update(override_fileds)
        pretained_config = PretrainedConfig.from_dict(config)
        pretained_config.set_rank(
            mapping.rank
        )  #TODO: remove the need of calling this, it's hacky design
        llama = cls(pretained_config)
        from .weight import load_from_meta_llama
        weights = load_from_meta_llama(meta_ckpt_dir, mapping, pretained_config)
        llama.load(weights)
        return llama
