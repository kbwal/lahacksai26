"""Generate a draft for one pair with a given model/adapter/arm.

    uv run python -m train.draft --model models/Qwen3.5-2B-Base --arm base \
        --adapter-path adapters/base --pairs data/valid.jsonl
"""

import argparse
import json
from pathlib import Path

from mlx_lm import generate, load

from gmail_agent.locking import process_lock
from gmail_agent.runtime import default_paths
from train.format import ARMS, Pair, prompt_ids


def draft(model, tokenizer, p: Pair, arm: str, max_tokens: int = 300) -> str:
    return generate(
        model, tokenizer, prompt=prompt_ids(tokenizer, p, arm), max_tokens=max_tokens
    ).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--arm", choices=ARMS, required=True)
    ap.add_argument("--adapter-path")
    ap.add_argument("--pairs", required=True)
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=300)
    ap.add_argument("--model-lock", type=Path, default=default_paths()["model_lock"])
    args = ap.parse_args()

    with process_lock(args.model_lock):
        model, tokenizer = load(args.model, adapter_path=args.adapter_path)
        with open(args.pairs) as f:
            pairs = [Pair.from_dict(json.loads(l)) for l in f if l.strip()][: args.limit]
        for p in pairs:
            print(f"=== To: {p.recipient} ({p.relationship})")
            print(f"--- actual:\n{p.reply}")
            print(f"--- draft:\n{draft(model, tokenizer, p, args.arm, args.max_tokens)}\n")


if __name__ == "__main__":
    main()
