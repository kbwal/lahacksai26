"""Draft messages with a given model/adapter/arm.

Against held-out data (drafts each test conversation's last "me" message from what came before it):

    uv run python -m train.draft --model models/Qwen3-1.7B-Base --arm base \\
        --adapter-path adapters/base/social --data data/conversations.jsonl

A new message from a request:

    uv run python -m train.draft --model models/Qwen3-1.7B-Base --arm base \\
        --adapter-path adapters/base/social --prompt "ask sarah to get dinner on tuesday"
"""

import argparse
import json
from dataclasses import replace
from pathlib import Path

from mlx_lm import generate, load

from train.format import ARMS, ME, SOURCES, Conversation, cut_turn, prompt_ids


def draft(model, tokenizer, c: Conversation, arm: str, max_tokens: int = 300) -> str:
    """The next "me" message after `c`."""
    return cut_turn(generate(model, tokenizer, prompt=prompt_ids(tokenizer, c, arm), max_tokens=max_tokens), arm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--arm", choices=ARMS, required=True)
    ap.add_argument("--adapter-path")
    ap.add_argument("--adapter-name", help="only conversations for this adapter (default: the --adapter-path directory name)")
    ap.add_argument("--data", help="JSONL of conversations")
    ap.add_argument("--type", choices=("train", "test"), help="only records of this type (default: test ones if there are any, else all)")
    ap.add_argument("--prompt", help="write a new message from this request instead")
    ap.add_argument("--source", choices=SOURCES, help="channel the --prompt message is for (default: none)")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=300)
    args = ap.parse_args()
    if not (args.data or args.prompt):
        ap.error("need --data or --prompt")

    model, tokenizer = load(args.model, adapter_path=args.adapter_path)
    if args.prompt:
        c = Conversation(adapter_name=args.adapter_name or "-", messages=[], prompt=args.prompt, source=args.source or "")
        print(draft(model, tokenizer, c, args.arm, args.max_tokens))
        return

    name = args.adapter_name or (Path(args.adapter_path).name if args.adapter_path else None)
    with open(args.data) as f:
        convs = [Conversation.from_dict(json.loads(l)) for l in f if l.strip()]
    convs = [c for c in convs if name is None or c.adapter_name == name]
    kind = args.type or ("test" if any(c.type == "test" for c in convs) else None)
    convs = [c for c in convs if kind is None or c.type == kind]
    shown = 0
    for c in convs:
        last = next((i for i in range(len(c.messages) - 1, -1, -1) if c.messages[i].sender == ME), None)
        if last is None:
            continue
        context = replace(c, messages=c.messages[:last])
        print(f"=== [{c.adapter_name}] {len(context.messages)} earlier messages" + (f", request: {c.prompt}" if c.prompt else "") + (f", on {c.source}" if c.source else ""))
        if context.messages:
            print(f"--- last from {context.messages[-1].sender}:\n{context.messages[-1].text}")
        print(f"--- actual:\n{c.messages[last].text}")
        print(f"--- draft:\n{draft(model, tokenizer, context, args.arm, args.max_tokens)}\n")
        shown += 1
        if shown == args.limit:
            break


if __name__ == "__main__":
    from gmail_agent.locking import process_lock
    from gmail_agent.runtime import default_paths

    with process_lock(default_paths()["model_lock"]):
        main()
