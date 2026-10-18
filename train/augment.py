"""Add a `prompt` to every conversation the user started, so the adapters learn
to write a new message from a request ("ask sarah to get dinner on tuesday"), not
only to reply.

The request is written by the local instruct model from the opening "me"
messages. It is a guess at what the user would have typed to get that message
written. Run this once, offline, on the raw (unmasked) data. The trainer then
keeps any fact the request mentions instead of masking it (see train/mask.py).
Conversations that start with "them", or already have a prompt, pass through
unchanged.

With --name, the user's sign-off ("Best,\nJeff") is removed before the model sees
the message. Otherwise a 1.7B model reads the sign-off as the recipient and writes
"email jeff ...". Existing prompts that mention the name or copy an example are regenerated. A new
prompt that still names the user, or just copies one of the examples (which it does
for messages with little content, like "see attached"), is dropped, since no prompt
is better than a wrong one.

    uv run python -m train.augment --model models/Qwen3-1.7B --name Jeff \\
        --data data/train.jsonl --out data/train.aug.jsonl
"""

import argparse
import json
import re

from mlx_lm import generate, load

from train.format import ME

SYSTEM = (
    "Turn a message I sent into the short request I would type to an AI assistant to get "
    "it written. Say who it's for (by a name from the message, or by who they are to me) "
    "and the main point, in one casual line. Reply with the request only."
)

# Few-shot as chat turns: a 1.7B model copies a single inline example instead.
EXAMPLES = [
    (
        "hey are we still on for basketball saturday? lmk if u need a ride",
        "text my friend to check we're still on for basketball saturday and offer a ride",
    ),
    (
        "Hi Maria,\n\nAttached is the Q3 report. Let me know if you have any questions.\n\nThanks,\nJ",
        "email maria the q3 report and ask if she has questions",
    ),
    (
        "Quick heads up, the launch is moving to next Friday so we have time for QA.",
        "tell the team the launch moved to next friday for QA",
    ),
]


def opening(d: dict) -> str:
    """The leading "me" messages of a record, or "" if it starts with "them"."""
    out = []
    for m in d.get("conversation", []):
        if m["from"] != ME:
            break
        out.append(m["text"].strip())
    return "\n\n".join(out)


def clean(text: str) -> str:
    line = next((l for l in text.strip().splitlines() if l.strip()), "")
    line = line.strip().strip("\"'`").strip()
    return line.removeprefix("Request:").strip()


def unsigned(message: str, name: str) -> str:
    """`message` without lines that are just the user's name, and the sign-off line before one."""
    signoff = rf"(?:[^\S\n]*\w+(?: \w+)?,[^\S\n]*\n)?[^\S\n]*-?{re.escape(name)}\.?[^\S\n]*$"
    return re.sub(signoff, "", message, flags=re.M | re.I).strip()


def _content_words(text: str) -> set[str]:
    return set(re.findall(r"\w{4,}", text.lower()))


def copies_example(prompt: str) -> bool:
    words = _content_words(prompt)
    return any(len(words & _content_words(r)) >= 0.5 * len(words) for _, r in EXAMPLES)


def request_for(model, tokenizer, message: str, max_chars: int = 4000) -> str:
    messages = [{"role": "system", "content": SYSTEM}]
    for example, request in EXAMPLES:
        messages += [{"role": "user", "content": example}, {"role": "assistant", "content": request}]
    messages.append({"role": "user", "content": message[:max_chars]})
    prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, enable_thinking=False)
    return clean(generate(model, tokenizer, prompt=prompt, max_tokens=60))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="an instruct model, e.g. models/Qwen3-1.7B")
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", help="the user's first name, as they sign messages")
    args = ap.parse_args()
    names_me = re.compile(rf"\b{re.escape(args.name)}\b", re.I) if args.name else None

    model, tokenizer = load(args.model)
    with open(args.data) as f:
        records = [json.loads(l) for l in f if l.strip()]
    added = dropped = 0
    with open(args.out, "w") as f:
        for d in records:
            old = d.get("prompt", "")
            if old and ((names_me and names_me.search(old)) or copies_example(old)):
                del d["prompt"]
            if not d.get("prompt") and (message := opening(d)):
                if args.name:
                    message = unsigned(message, args.name)
                prompt = request_for(model, tokenizer, message) if message else ""
                if not prompt or (names_me and names_me.search(prompt)) or copies_example(prompt):
                    dropped += 1
                    print(f"dropped {prompt!r}")
                else:
                    d["prompt"] = prompt
                    added += 1
                    print(f"{prompt!r}  <-  {message[:80]!r}")
            f.write(json.dumps(d) + "\n")
    print(f"{added} of {len(records)} conversations got a new prompt, {dropped} dropped (named the user or copied an example) -> {args.out}")


if __name__ == "__main__":
    main()
