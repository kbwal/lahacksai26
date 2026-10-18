"""Prompt rendering shared by training, eval and the server.

A conversation is a chain of messages on any channel (email, Slack, texts), each
either from "me" (the user, whose voice we learn) or from "them" (anyone else).
Loss goes on the "me" messages only. One JSONL record per conversation:

    {"adapter_name": "social",
     "type": "train",
     "source": "slack",
     "prompt": "ask sarah to get dinner on tuesday",
     "conversation": [{"from": "me", "text": "..."}, {"from": "them", "text": "..."}, ...]}

`adapter_name` picks the LoRA adapter (persona) the record trains. `type` is
"train" (the default) or "test": test records are held out and only evaluated
on. `source` is the channel the conversation happened on (one of SOURCES), and
is optional; it goes in the header, so the model can learn how the user writes
differently on each one. `prompt` is
optional, and only allowed when the conversation starts with "me". It is the
request the user would type to get that opening message written (see
train/augment.py), so the same adapter can also write new messages, not just
replies.

Base arm, plain text. Every "me" message ends with <|endoftext|>:

    Source: slack
    Request: ask sarah to get dinner on tuesday

    Me: hey! dinner [DATE]?<|endoftext|>

    Them: yes!! where

    Me: ...

Instruct arm, ChatML. "them" is the user role, "me" the assistant, and the
source and request go in the system prompt. Every assistant turn carries the empty think
block that `enable_thinking=False` adds to the generation prompt. The stock
template drops it from earlier turns, which would make a message trained as
history look different from one being generated.

Each piece is tokenized separately and the ids are concatenated. So the tokens
before a "me" message at training time are exactly the ones fed at inference
time (no boundary-merge surprises).

With a token budget, the oldest messages are dropped (the oldest one kept may be
cut from the start). The header and the message being written are never cut.
"""

import re
from dataclasses import dataclass, replace
from typing import NamedTuple

from train.mask import mask_reply

ARMS = ("base", "instruct")
ME, THEM = "me", "them"
TYPES = ("train", "test")
SOURCES = ("gmail", "slack", "discord")

SYSTEM_PROMPT = (
    "Write the user's next message in their own voice. Messages from the other people "
    "in the conversation appear as user turns. Use placeholders such as [DATE], [TIME], "
    "[AMOUNT], [PHONE], [ADDRESS] and [LINK] for any fact you don't know."
)

Example = tuple[list[int], list[int]]  # (tokens, loss mask: 1 on the "me" message tokens)


@dataclass
class Message:
    sender: str  # ME or THEM
    text: str

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        if d["from"] not in (ME, THEM):
            raise ValueError(f"message 'from' must be {ME!r} or {THEM!r}, got {d['from']!r}")
        return cls(sender=d["from"], text=d["text"])


@dataclass
class Conversation:
    adapter_name: str
    messages: list[Message]
    prompt: str = ""
    type: str = "train"
    source: str = ""  # one of SOURCES, or "" when unknown

    @classmethod
    def from_dict(cls, d: dict) -> "Conversation":
        name = d["adapter_name"]
        # It names the adapter's directory.
        if not re.fullmatch(r"[\w-]+", name):
            raise ValueError(f"adapter_name must be letters, digits, _ or -, got {name!r}")
        messages = [Message.from_dict(m) for m in d.get("conversation", [])]
        prompt = d.get("prompt") or ""
        # The request is for the user's opening message. An empty conversation is
        # fine: that is a new message being written from the request.
        if prompt and messages and messages[0].sender != ME:
            raise ValueError("a prompt only goes on a conversation that starts with a 'me' message")
        kind = d.get("type") or "train"
        if kind not in TYPES:
            raise ValueError(f"type must be one of {TYPES}, got {kind!r}")
        source = d.get("source") or ""
        if source and source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
        return cls(adapter_name=name, messages=messages, prompt=prompt, type=kind, source=source)


def masked(c: Conversation) -> Conversation:
    """`c` with the facts in its "me" messages replaced by placeholders (see train/mask.py).

    Each one may echo facts from earlier "them" messages, and any fact the request names.
    """
    seen, out = [], []
    for m in c.messages:
        if m.sender == ME:
            m = replace(m, text=mask_reply(m.text, "\n".join(seen), request=c.prompt))
        else:
            seen.append(m.text)
        out.append(m)
    return replace(c, messages=out)


def _check(arm: str):
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}")


def _ids(tokenizer, text: str) -> list[int]:
    return tokenizer.encode(text, add_special_tokens=False)


def _header(tokenizer, c: Conversation, arm: str) -> list[int]:
    lines = [f"Source: {c.source}"] if c.source else []
    lines += [f"Request: {c.prompt}"] if c.prompt else []
    if arm == "base":
        return _ids(tokenizer, "\n".join(lines))
    system = SYSTEM_PROMPT + ("\n\n" + "\n".join(lines) if lines else "")
    return _ids(tokenizer, f"<|im_start|>system\n{system}<|im_end|>")


def _sep(tokenizer, arm: str) -> list[int]:
    return _ids(tokenizer, "\n\n" if arm == "base" else "\n")


def _opening(tokenizer, sender: str, arm: str) -> list[int]:
    """What comes before a message's text."""
    if arm == "base":
        return _ids(tokenizer, "Me:" if sender == ME else "Them:")
    if sender == ME:
        return _ids(tokenizer, "<|im_start|>assistant\n<think>\n\n</think>\n\n")
    return _ids(tokenizer, "<|im_start|>user\n")


def _body(tokenizer, sender: str, text: str, arm: str) -> list[int]:
    """A message's text and what closes it. For "me" that is the token generation
    stops on, so it is trained along with the text."""
    if arm == "base":
        ids = _ids(tokenizer, " " + text.strip())
        return ids + [tokenizer.eos_token_id] if sender == ME else ids
    return _ids(tokenizer, text.strip()) + _ids(tokenizer, "<|im_end|>")


def cut_turn(text: str, arm: str) -> str:
    """A generated "me" message, cut where the model started writing the next turn
    (a backstop for a base model that hasn't learned to emit <|endoftext|> yet)."""
    if arm == "base":
        text = re.split(r"\n\s*(?:Me|Them):", text, maxsplit=1)[0]
    return text.strip()


class _Turn(NamedTuple):
    opening: list[int]
    body: list[int]
    trained: bool


def _fit(tokenizer, arm: str, header: list[int], turns: list[_Turn], budget: int | None) -> tuple[Example, int] | None:
    """`header` and `turns` laid out as one example, and the index of the oldest turn kept whole.

    With a budget, turns are kept newest first. The first one that doesn't fit is
    cut from the start to fill what's left (and not trained), and older ones are
    dropped. None if the last turn doesn't fit whole.
    """
    # In the base arm a message's text starts with the space after "Me:" / "Them:".
    sep, ellipsis = _sep(tokenizer, arm), _ids(tokenizer, " …" if arm == "base" else "…")
    kept, used, first = [], len(header), len(turns)
    for i in range(len(turns) - 1, -1, -1):
        t = turns[i]
        cost = len(sep) + len(t.opening) + len(t.body)
        if budget is None or used + cost <= budget:
            kept.append(t)
            used += cost
            first = i
            continue
        if i == len(turns) - 1:
            return None
        room = budget - used - len(sep) - len(t.opening) - len(ellipsis)
        if room > 0:
            kept.append(_Turn(t.opening, ellipsis + t.body[-room:], False))
        break

    tokens, mask = list(header), [0] * len(header)
    for t in reversed(kept):
        if tokens:
            tokens += sep
            mask += [0] * len(sep)
        tokens += t.opening + t.body
        mask += [0] * len(t.opening) + [int(t.trained)] * len(t.body)
    return (tokens, mask), first


def _turns(tokenizer, c: Conversation, arm: str, train: bool) -> list[_Turn]:
    return [
        _Turn(_opening(tokenizer, m.sender, arm), _body(tokenizer, m.sender, m.text, arm), train and m.sender == ME)
        for m in c.messages
    ]


def prompt_ids(tokenizer, c: Conversation, arm: str, budget: int | None = None) -> list[int] | None:
    """Tokens to generate the next "me" message after `c` from: its newest messages
    that fit in `budget`, then the opening of a "me" message. None if nothing fits."""
    _check(arm)
    turns = _turns(tokenizer, c, arm, train=False) + [_Turn(_opening(tokenizer, ME, arm), [], False)]
    fit = _fit(tokenizer, arm, _header(tokenizer, c, arm), turns, budget)
    return None if fit is None else fit[0][0]


def training_examples(tokenizer, c: Conversation, arm: str, max_len: int | None = None) -> tuple[list[Example], int]:
    """Examples that put loss on every "me" message in `c`, and how many "me" messages
    were dropped because they don't fit in `max_len` even on their own.

    If the whole conversation fits, that is one example. Otherwise each example
    ends at a "me" message with as much history as fits, so every message is
    trained exactly once.
    """
    _check(arm)
    header, turns = _header(tokenizer, c, arm), _turns(tokenizer, c, arm, train=True)
    examples, dropped, end = [], 0, len(turns)
    while (last := next((i for i in range(end - 1, -1, -1) if turns[i].trained), None)) is not None:
        fit = _fit(tokenizer, arm, header, turns[: last + 1], max_len)
        if fit is None:
            dropped += 1
            end = last
        else:
            examples.append(fit[0])
            end = fit[1]
    examples.reverse()
    return examples, dropped


@dataclass
class Edit:
    """A model draft of the next "me" message after `context`, and what the user actually sent."""

    context: Conversation
    draft: str
    final: str

    @property
    def adapter_name(self) -> str:
        return self.context.adapter_name

    @property
    def type(self) -> str:
        return self.context.type

    @classmethod
    def from_dict(cls, d: dict) -> "Edit":
        return cls(context=Conversation.from_dict(d), draft=d["draft"], final=d["final"])


def masked_edit(e: Edit) -> Edit:
    """`e` with its final (and its context's "me" messages) masked. The draft is left as the
    model wrote it, so DPO pushes down any specifics it invented."""
    c = masked(replace(e.context, messages=[*e.context.messages, Message(ME, e.final)]))
    return replace(e, context=replace(c, messages=c.messages[:-1]), final=c.messages[-1].text)


def _complete(prompt: list[int], body: list[int]) -> Example:
    return prompt + body, [0] * len(prompt) + [1] * len(body)


def reply_example(tokenizer, c: Conversation, reply: str, arm: str, max_len: int | None = None) -> Example | None:
    """Loss on `reply` as the next "me" message after `c`, with `c` trimmed to fit `max_len`."""
    body = _body(tokenizer, ME, reply, arm)
    prompt = prompt_ids(tokenizer, c, arm, None if max_len is None else max_len - len(body))
    return None if prompt is None else _complete(prompt, body)


def preference_example(tokenizer, e: Edit, arm: str, max_len: int | None = None) -> tuple[Example, Example] | None:
    """(chosen, rejected): the sent final vs. the model draft, after one shared prompt
    trimmed to fit the longer of the two in `max_len`."""
    chosen, rejected = _body(tokenizer, ME, e.final, arm), _body(tokenizer, ME, e.draft, arm)
    budget = None if max_len is None else max_len - max(len(chosen), len(rejected))
    prompt = prompt_ids(tokenizer, e.context, arm, budget)
    return None if prompt is None else (_complete(prompt, chosen), _complete(prompt, rejected))
