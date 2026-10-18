"""Prompt rendering shared by training, eval and the server.

Both arms tokenize the prompt and the reply separately and concatenate the ids,
so the prompt tokens seen at training time are exactly the ones fed at
inference time (no boundary-merge surprises).
"""

from dataclasses import dataclass

from message_pipeline import SOURCES

ARMS = ("base", "instruct")

SYSTEM_PROMPT = (
    "Draft the reply in the user's own voice. Use placeholders such as [DATE], "
    "[TIME], [AMOUNT], [PHONE], [ADDRESS] and [LINK] for any fact you don't know."
)


@dataclass
class Pair:
    recipient: str
    relationship: str
    thread: str
    reply: str = ""
    source: str = "gmail"

    def __post_init__(self):
        if self.source not in SOURCES:
            raise ValueError("Unknown message source")

    @classmethod
    def from_dict(cls, d: dict) -> "Pair":
        return cls(
            recipient=d["recipient"],
            relationship=d.get("relationship") or "unknown",
            thread=d.get("thread") or "(new email)",
            reply=d.get("reply", ""),
            source=d.get("source", "gmail"),
        )


def prompt_text(p: Pair) -> str:
    return f"Source: {p.source}\nTo: {p.recipient} ({p.relationship})\nThread: {p.thread}\nReply:"


def _encode(tokenizer, text: str) -> list[int]:
    return tokenizer.encode(text, add_special_tokens=False)


def prompt_ids(tokenizer, p: Pair, arm: str) -> list[int]:
    if arm == "base":
        return _encode(tokenizer, prompt_text(p))
    if arm == "instruct":
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_text(p)},
        ]
        text = tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            enable_thinking=False,
            tokenize=False,
        )
        return _encode(tokenizer, text)
    raise ValueError(f"unknown arm {arm!r}")


def completion_ids(tokenizer, p: Pair, arm: str) -> list[int]:
    if arm == "base":
        # Base prompt ends with "Reply:", so the reply starts after a space.
        return _encode(tokenizer, " " + p.reply.strip()) + [tokenizer.eos_token_id]
    if arm == "instruct":
        im_end = tokenizer.convert_tokens_to_ids("<|im_end|>")
        return _encode(tokenizer, p.reply.strip()) + [im_end]
    raise ValueError(f"unknown arm {arm!r}")


def training_example(tokenizer, p: Pair, arm: str) -> tuple[list[int], int]:
    """(tokens, prompt_len) in the shape mlx_lm.tuner.trainer.iterate_batches expects."""
    prompt = prompt_ids(tokenizer, p, arm)
    return prompt + completion_ids(tokenizer, p, arm), len(prompt)
