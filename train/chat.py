"""Talk to an adapter: you write the other person's messages, it replies as the user.

    uv run python -m train.chat --adapter-path train/data/enron/adapters/base/professional

The model and arm are read from the adapter's config. Type a message and press
enter; `\\n` in what you type becomes a line break. Commands:

    /request <text>   start over and write a new message from a request
    /new              start over
    /compare          also show what the model without the adapter writes (toggle)
    /temp <x>         sampling temperature (0 = greedy, which tends to loop)
    /history          show the conversation so far
    /quit
"""

import argparse
import json
import readline  # noqa: F401  (line editing and history for input())
from dataclasses import replace
from pathlib import Path

from mlx_lm import generate, load
from mlx_lm.sample_utils import make_sampler

from train.format import ARMS, ME, SOURCES, THEM, Conversation, Message, cut_turn, prompt_ids


def reply(model, tokenizer, c: Conversation, arm: str, temp: float, max_tokens: int, max_len: int) -> str:
    prompt = prompt_ids(tokenizer, c, arm, budget=max_len - max_tokens)
    if prompt is None:
        return "(the conversation is too long to fit; /new to start over)"
    text = generate(model, tokenizer, prompt=prompt, max_tokens=max_tokens, sampler=make_sampler(temp=temp, top_p=0.95))
    return cut_turn(text, arm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter-path", required=True)
    ap.add_argument("--model", help="default: the model the adapter was trained on")
    ap.add_argument("--arm", choices=ARMS, help="default: the arm the adapter was trained with")
    ap.add_argument("--source", choices=SOURCES, help="channel the conversation is on (default: none)")
    ap.add_argument("--temp", type=float, default=0.7)
    ap.add_argument("--max-tokens", type=int, default=300)
    ap.add_argument("--max-len", type=int, default=2048, help="tokens of context kept (oldest messages dropped first)")
    args = ap.parse_args()

    config = json.loads((Path(args.adapter_path) / "adapter_config.json").read_text())
    model_path, arm = args.model or config["model"], args.arm or config["arm"]
    name = config.get("adapter_name", Path(args.adapter_path).name)
    print(f"loading {model_path} + {name} adapter ({arm} arm)...")
    models = {name: load(model_path, adapter_path=args.adapter_path)}
    temp, compare = args.temp, False

    def respond(c: Conversation) -> Conversation:
        # Only the adapter's reply goes into the conversation.
        mine = reply(*models[name], c, arm, temp, args.max_tokens, args.max_len)
        if not compare:
            print(f"\nme: {mine}\n")
            return replace(c, messages=[*c.messages, Message(ME, mine)])
        print(f"\n[{name}]\n{mine}\n")
        print(f"[no adapter]\n{reply(*models['no adapter'], c, arm, temp, args.max_tokens, args.max_len)}\n")
        return replace(c, messages=[*c.messages, Message(ME, mine)])

    c = Conversation(name, [], source=args.source or "")
    print("Commands:\n" + __doc__.split("Commands:\n\n")[1].rstrip() + "\n")
    while True:
        try:
            line = input("them> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        if cmd == "/quit":
            break
        elif cmd == "/new":
            c = Conversation(name, [], source=args.source or "")
            print("(new conversation)")
        elif cmd == "/request":
            c = respond(Conversation(name, [], prompt=rest, source=args.source or ""))
        elif cmd == "/compare":
            compare = not compare
            if compare and "no adapter" not in models:
                print(f"loading {model_path} without the adapter...")
                models["no adapter"] = load(model_path)
            print(f"(compare {'on' if compare else 'off'})")
        elif cmd == "/temp":
            temp = float(rest)
            print(f"(temp {temp})")
        elif cmd == "/history":
            if c.source:
                print(f"source: {c.source}")
            if c.prompt:
                print(f"request: {c.prompt}")
            for m in c.messages:
                print(f"{m.sender}: {m.text}\n")
        else:
            c = respond(replace(c, messages=[*c.messages, Message(THEM, line.replace("\\n", "\n"))]))


if __name__ == "__main__":
    main()
