"""Label every conversation with a persona, and add a `prompt` to every
conversation the user started, so the adapters learn to write a new message from
a request ("ask sarah to get dinner on tuesday"), not only to reply.

The persona (`adapter_name`) is "professional" or "social". The instruct model
reads the conversation and we compare the scores of the two words as its answer,
so the label is always one of them. Records that already have an
`adapter_name` keep it unless --relabel is given (e.g. to replace the recipient
rule in pipeline/enron.py).

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
        --data data/train.jsonl --out data/train.aug.jsonl [--relabel]

With --endpoint, the same questions go to an OpenAI-compatible chat endpoint serving
an instruct model instead of the local one (e.g. Qwen3-4B on a Veri deployment, see
train/veri.py), which is much faster than a laptop. The persona is still whichever
label scores higher, from the first answer token's logprobs.

    uv run python -m train.augment --endpoint https://api.veri.studio/v1/deployments/<id> \\
        --name Jeff --data data/train.jsonl --out data/train.aug.jsonl --relabel
"""

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter

import mlx.core as mx
from mlx_lm import generate, load

from train.format import ME

PERSONAS = ("professional", "social")

# Ask about the relationship, not the tone: asked "is this professional or social?",
# the 1.7B model called nearly every email professional, love letters included.
PERSONA_SYSTEM = (
    "Who am I talking to in this conversation? Answer professional if it's a coworker, client, "
    "classmate, teacher or company, and social if it's a friend, family member or partner. "
    "Answer with one word: professional or social."
)

PERSONA_EXAMPLES = [
    (
        "Them: Hi, could you send the Q3 numbers before the board meeting?\n\n"
        "Me: Sure, attached. Let me know if anything looks off.\n\nThanks,\nJ",
        "professional",
    ),
    ("Me: hey are we still on for basketball saturday? lmk if u need a ride\n\nThem: yes!! pick me up at 9", "social"),
    ("Me: Dear Professor Lee,\n\nWould you have time to meet next week to discuss my project?", "professional"),
    ("Me: Mom, my flight lands at 6. Can you pick me up?", "social"),
]

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


def transcript(d: dict, max_chars: int = 4000) -> str:
    """The record as "Me: ..." / "Them: ..." lines, or its request if it has no messages."""
    lines = [f"{'Me' if m['from'] == ME else 'Them'}: {m['text'].strip()}" for m in d.get("conversation", [])]
    text = "\n\n".join(lines) or (f"Request: {d['prompt']}" if d.get("prompt") else "")
    return text[:max_chars]


def _few_shot(system: str, examples: list[tuple[str, str]], text: str) -> list[dict]:
    messages = [{"role": "system", "content": system}]
    for example, answer in examples:
        messages += [{"role": "user", "content": example}, {"role": "assistant", "content": answer}]
    return messages + [{"role": "user", "content": text}]


def persona_for(model, tokenizer, text: str) -> str:
    """Whichever of PERSONAS the model scores higher as the first token of its answer."""
    messages = _few_shot(PERSONA_SYSTEM, PERSONA_EXAMPLES, text)
    ids = tokenizer.apply_chat_template(messages, add_generation_prompt=True, enable_thinking=False)
    firsts = [tokenizer.encode(p, add_special_tokens=False)[0] for p in PERSONAS]
    assert len(set(firsts)) == len(PERSONAS), "persona labels must start with different tokens"
    logits = model(mx.array(ids)[None])[0, -1]
    return PERSONAS[int(mx.argmax(logits[mx.array(firsts)]).item())]


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
    messages = _few_shot(SYSTEM, EXAMPLES, message[:max_chars])
    prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, enable_thinking=False)
    return clean(generate(model, tokenizer, prompt=prompt, max_tokens=60))


class Endpoint:
    """An OpenAI-compatible chat endpoint standing in for the local model: `persona_for` and
    `request_for` with the same prompts. Waits out rate limits and a waking deployment."""

    def __init__(self, url: str, api_key: str):
        self.url, self.api_key = url.rstrip("/") + "/chat/completions", api_key

    def chat(self, messages: list[dict], **params) -> dict:
        # Qwen3 is hybrid-thinking: answer directly, as the local path does.
        thinking_off = {"chat_template_kwargs": {"enable_thinking": False}}
        body = json.dumps({"model": "default", "messages": messages, "temperature": 0, **thinking_off, **params}).encode()
        for attempt in range(8):
            req = urllib.request.Request(
                self.url, body, {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code not in (429, 503) or attempt == 7:
                    raise RuntimeError(f"{self.url}: HTTP {e.code} {e.read().decode(errors='replace')[:500]}") from None
                time.sleep(float(e.headers.get("Retry-After") or 5))

    def persona_for(self, text: str) -> str | None:
        """The label with the higher first-token logprob, or None if neither is in the top 20."""
        out = self.chat(_few_shot(PERSONA_SYSTEM, PERSONA_EXAMPLES, text), max_tokens=1, logprobs=True, top_logprobs=20)
        scores = {p: float("-inf") for p in PERSONAS}
        for t in out["choices"][0]["logprobs"]["content"][0]["top_logprobs"]:
            for p in PERSONAS:
                if t["token"].strip() and p.startswith(t["token"].strip()):
                    scores[p] = max(scores[p], t["logprob"])
        best = max(PERSONAS, key=scores.get)
        return best if scores[best] > float("-inf") else None

    def request_for(self, message: str, max_chars: int = 4000) -> str:
        out = self.chat(_few_shot(SYSTEM, EXAMPLES, message[:max_chars]), max_tokens=60)
        return clean(out["choices"][0]["message"]["content"] or "")


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--model", help="a local instruct model, e.g. models/Qwen3-1.7B")
    src.add_argument("--endpoint", help="an OpenAI-compatible chat endpoint, e.g. https://api.veri.studio/v1/deployments/<id>")
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", help="the user's first name, as they sign messages")
    ap.add_argument("--relabel", action="store_true", help="also re-label records that already have an adapter_name")
    args = ap.parse_args()
    names_me = re.compile(rf"\b{re.escape(args.name)}\b", re.I) if args.name else None

    if args.endpoint:
        from train.veri import api_key

        llm = Endpoint(args.endpoint, api_key())
        persona, request = llm.persona_for, llm.request_for
    else:
        model, tokenizer = load(args.model)
        persona = lambda text: persona_for(model, tokenizer, text)
        request = lambda message: request_for(model, tokenizer, message)
    with open(args.data) as f:
        records = [json.loads(l) for l in f if l.strip()]
    added = dropped = 0
    labels = Counter()  # (old adapter_name or None, new one)
    with open(args.out, "w") as f:
        for d in records:
            if (args.relabel or not d.get("adapter_name")) and (text := transcript(d)):
                if name := persona(text):
                    labels[d.get("adapter_name"), name] += 1
                    d = {"adapter_name": name, **{k: v for k, v in d.items() if k != "adapter_name"}}
                else:
                    print(f"no persona label in the endpoint's top tokens, kept {d.get('adapter_name')!r}")
            old = d.get("prompt", "")
            if old and ((names_me and names_me.search(old)) or copies_example(old)):
                del d["prompt"]
            if not d.get("prompt") and (message := opening(d)):
                if args.name:
                    message = unsigned(message, args.name)
                prompt = request(message) if message else ""
                if not prompt or (names_me and names_me.search(prompt)) or copies_example(prompt):
                    dropped += 1
                    print(f"dropped {prompt!r}")
                else:
                    d["prompt"] = prompt
                    added += 1
                    print(f"{prompt!r}  <-  {message[:80]!r}")
            f.write(json.dumps(d) + "\n")
    print(f"{sum(labels.values())} of {len(records)} conversations labeled with a persona:")
    for (old, new), n in sorted(labels.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        print(f"  {old or '(none)':12} -> {new:12} {n}")
    print(f"{added} of {len(records)} conversations got a new prompt, {dropped} dropped (named the user or copied an example) -> {args.out}")


if __name__ == "__main__":
    main()
