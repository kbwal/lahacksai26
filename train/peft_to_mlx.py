"""Convert a Hugging Face PEFT LoRA adapter (what a Veri job trains) into an mlx-lm one.

    uv run python -m train.peft_to_mlx <peft dir> <mlx adapter dir> --model models/Qwen3-4B-Base \\
        --arm base --adapter-name social

PEFT adds (alpha / rank) * x @ A.T @ B.T to a linear layer, mlx-lm adds
scale * x @ lora_a @ lora_b, so lora_a = A.T, lora_b = B.T and scale = alpha / rank
(alpha / sqrt(rank) with rsLoRA). The output's adapter_config.json lists every converted
module by its full path in `keys`, so mlx-lm wraps exactly those layers, wherever they
are, instead of the last `num_layers` blocks that train/trainer.py uses.
"""

import argparse
import json
import math
import re
from pathlib import Path

import mlx.core as mx
from mlx_lm.utils import save_config

PEFT_KEY = re.compile(r"^base_model\.model\.(?P<module>.+)\.lora_(?P<ab>[AB])(?:\.default)?\.weight$")


def convert_weights(weights: dict[str, mx.array]) -> dict[str, mx.array]:
    """PEFT tensors -> mlx-lm tensors, keyed by the module's path in the mlx model."""
    out = {}
    for key, w in weights.items():
        m = PEFT_KEY.match(key)
        if not m:
            raise ValueError(f"not a LoRA A/B weight, can't convert: {key}")
        out[f"{m['module']}.lora_{m['ab'].lower()}"] = w.T.astype(mx.float32)
    return out


def lora_parameters(peft_config: dict) -> dict:
    """mlx-lm `lora_parameters` for a PEFT adapter_config.json (one rank and alpha for all layers)."""
    if peft_config.get("peft_type", "LORA") != "LORA":
        raise ValueError(f"only LoRA adapters convert, got {peft_config['peft_type']}")
    for unsupported in ("rank_pattern", "alpha_pattern", "modules_to_save"):
        if peft_config.get(unsupported):
            raise ValueError(f"adapters with {unsupported} aren't supported")
    if peft_config.get("use_dora"):
        raise ValueError("DoRA adapters aren't supported")
    rank, alpha = peft_config["r"], peft_config.get("lora_alpha") or peft_config["r"]
    scale = alpha / (math.sqrt(rank) if peft_config.get("use_rslora") else rank)
    return {"rank": rank, "scale": scale, "dropout": 0.0}


def convert(peft_dir: Path, out: Path, model: str, arm: str, adapter_name: str) -> int:
    """Write the mlx-lm adapter for `peft_dir` to `out`; returns the number of LoRA modules."""
    peft_config = json.loads((peft_dir / "adapter_config.json").read_text())
    trained = peft_config.get("base_model_name_or_path")
    if trained and Path(trained).name != Path(model).name:
        raise ValueError(
            f"The PEFT adapter was trained on {trained}, not {model}. "
            "Use a local copy of its training base; DPO on a merged SFT model needs that merged base."
        )
    parameters = lora_parameters(peft_config)
    weights = convert_weights(mx.load(str(peft_dir / "adapter_model.safetensors")))
    modules = sorted({k.rsplit(".", 1)[0] for k in weights})
    layers = {int(m) for m in re.findall(r"\.layers\.(\d+)\.", " ".join(modules))}

    out.mkdir(parents=True, exist_ok=True)
    mx.save_safetensors(str(out / "adapters.safetensors"), weights)
    save_config(
        {
            "fine_tune_type": "lora",
            # Full-path keys are matched against the whole model, so this only has
            # to be a valid count; 0 would mean every block in mlx-lm.
            "num_layers": max(layers, default=0) + 1,
            "lora_parameters": parameters | {"keys": modules},
            "model": model,
            "arm": arm,
            "adapter_name": adapter_name,
            "converted_from": str(peft_dir),
        },
        out / "adapter_config.json",
    )
    print(f"{len(modules)} LoRA modules in {len(layers)} layers -> {out}")
    return len(modules)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("peft_dir", help="directory with adapter_config.json and adapter_model.safetensors")
    ap.add_argument("out_dir")
    ap.add_argument("--model", required=True, help="the local base model the adapter will be loaded on")
    ap.add_argument("--arm", required=True)
    ap.add_argument("--adapter-name", required=True)
    args = ap.parse_args()
    convert(Path(args.peft_dir), Path(args.out_dir), args.model, args.arm, args.adapter_name)


if __name__ == "__main__":
    main()
