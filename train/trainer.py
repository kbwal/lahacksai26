"""LoRA SFT for both arms on top of mlx_lm.tuner.

mlx-lm's `completions` dataset always applies the chat template, which would
defeat the base arm, so we build (tokens, prompt_len) examples ourselves and
reuse mlx-lm's batching, masked loss and training loop.

    uv run python -m train.trainer --model models/Qwen3.5-2B-Base --arm base \
        --train data/train.jsonl --valid data/valid.jsonl --adapter-path adapters/base
"""

import argparse
import json
from pathlib import Path

import mlx.core as mx
import mlx.optimizers as optim
from mlx_lm import load
from mlx_lm.tuner.trainer import TrainingArgs, train
from mlx_lm.tuner.utils import linear_to_lora_layers
from mlx_lm.utils import save_config

from train.format import ARMS, Pair, training_example


def load_examples(tokenizer, path: str, arm: str) -> list[tuple[list[int], int]]:
    with open(path) as f:
        return [training_example(tokenizer, Pair.from_dict(json.loads(l)), arm) for l in f if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--arm", choices=ARMS, required=True)
    ap.add_argument("--train", required=True)
    ap.add_argument("--valid")
    ap.add_argument("--adapter-path", required=True)
    ap.add_argument("--iters", type=int, default=200)
    ap.add_argument("--batch-size", type=int, default=4)
    ap.add_argument("--learning-rate", type=float, default=1e-5)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--num-layers", type=int, default=16)
    ap.add_argument("--max-seq-length", type=int, default=2048)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    mx.random.seed(args.seed)
    model, tokenizer = load(args.model)
    train_set = load_examples(tokenizer, args.train, args.arm)
    valid_set = load_examples(tokenizer, args.valid, args.arm) if args.valid else None

    lora_parameters = {"rank": args.rank, "scale": 20.0, "dropout": 0.0}
    model.freeze()
    linear_to_lora_layers(model, args.num_layers, lora_parameters)

    adapter_path = Path(args.adapter_path)
    adapter_path.mkdir(parents=True, exist_ok=True)
    # First three keys are what mlx_lm.load(..., adapter_path=...) reads back.
    save_config(
        {
            "fine_tune_type": "lora",
            "num_layers": args.num_layers,
            "lora_parameters": lora_parameters,
            "model": args.model,
            "arm": args.arm,
        },
        adapter_path / "adapter_config.json",
    )

    train(
        model=model,
        optimizer=optim.Adam(learning_rate=args.learning_rate),
        train_dataset=train_set,
        val_dataset=valid_set,
        args=TrainingArgs(
            batch_size=args.batch_size,
            iters=args.iters,
            steps_per_eval=50 if valid_set else args.iters + 1,
            steps_per_save=args.iters,
            adapter_file=str(adapter_path / "adapters.safetensors"),
            max_seq_length=args.max_seq_length,
            grad_checkpoint=True,
        ),
    )


if __name__ == "__main__":
    main()
