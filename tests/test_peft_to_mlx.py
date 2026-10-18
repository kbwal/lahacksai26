import mlx.core as mx
import mlx.nn as nn
import numpy as np
import pytest
from mlx_lm.tuner.lora import LoRALinear

from train.peft_to_mlx import convert_weights, lora_parameters


def test_converted_lora_matches_peft_output():
    rank, alpha, d_in, d_out = 4, 32, 8, 6
    A, B = mx.random.normal((rank, d_in)), mx.random.normal((d_out, rank))
    peft = {
        "base_model.model.model.layers.3.self_attn.q_proj.lora_A.weight": A,
        "base_model.model.model.layers.3.self_attn.q_proj.lora_B.weight": B,
    }
    w = convert_weights(peft)
    assert set(w) == {"model.layers.3.self_attn.q_proj.lora_a", "model.layers.3.self_attn.q_proj.lora_b"}

    params = lora_parameters({"r": rank, "lora_alpha": alpha})
    base = nn.Linear(d_in, d_out)
    layer = LoRALinear.from_base(base, r=rank, scale=params["scale"])
    layer.lora_a, layer.lora_b = w["model.layers.3.self_attn.q_proj.lora_a"], w["model.layers.3.self_attn.q_proj.lora_b"]

    x = mx.random.normal((2, d_in))
    expected = base(x) + (alpha / rank) * (x @ A.T @ B.T)
    assert np.allclose(np.array(layer(x)), np.array(expected), atol=1e-4)


def test_default_adapter_name_and_rslora():
    w = convert_weights({"base_model.model.model.layers.0.mlp.up_proj.lora_A.default.weight": mx.zeros((2, 3))})
    assert w["model.layers.0.mlp.up_proj.lora_a"].shape == (3, 2)
    assert lora_parameters({"r": 16, "lora_alpha": 32, "use_rslora": True})["scale"] == 8.0
    assert lora_parameters({"r": 16})["scale"] == 1.0


@pytest.mark.parametrize("bad", [{"rank_pattern": {"q_proj": 8}}, {"use_dora": True}, {"modules_to_save": ["lm_head"]}])
def test_rejects_what_it_cant_convert(bad):
    with pytest.raises(ValueError):
        lora_parameters({"r": 16, "lora_alpha": 32} | bad)


def test_rejects_non_lora_weights():
    with pytest.raises(ValueError):
        convert_weights({"base_model.model.lm_head.weight": mx.zeros((2, 2))})
