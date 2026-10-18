from dataclasses import replace
from pathlib import Path

import pytest

from train.format import (
    ME,
    SYSTEM_PROMPT,
    THEM,
    Conversation,
    Edit,
    Message,
    cut_turn,
    masked,
    masked_edit,
    preference_example,
    prompt_ids,
    reply_example,
    training_examples,
)


class CharTokenizer:
    """One token per character, enough to test layout and length budgeting."""

    eos_token_id = 0

    def encode(self, text, add_special_tokens=False):
        return [ord(c) for c in text]

    def decode(self, ids):
        return "".join(chr(i) for i in ids)


EOS = chr(0)


def conv(*messages, prompt="", name="social"):
    return Conversation(name, [Message(s, t) for s, t in messages], prompt)


def trained_text(tok, example):
    tokens, mask = example
    return tok.decode([t for t, m in zip(tokens, mask) if m])


def test_from_dict():
    c = Conversation.from_dict(
        {"adapter_name": "pro", "prompt": "say hi", "conversation": [{"from": "me", "text": "hi"}, {"from": "them", "text": "yo"}]}
    )
    assert c == conv((ME, "hi"), (THEM, "yo"), prompt="say hi", name="pro")


@pytest.mark.parametrize(
    "d",
    [
        {"adapter_name": "pro", "conversation": [{"from": "you", "text": "hi"}]},
        {"adapter_name": "../pro", "conversation": []},
        {"adapter_name": "pro", "prompt": "say hi", "conversation": [{"from": "them", "text": "hi"}]},
        {"adapter_name": "pro", "source": "dasovich-j/sent/17."},
    ],
)
def test_from_dict_rejects(d):
    with pytest.raises(ValueError):
        Conversation.from_dict(d)


def test_type_defaults_to_train():
    assert Conversation.from_dict({"adapter_name": "pro"}).type == "train"
    assert Conversation.from_dict({"adapter_name": "pro", "type": "test"}).type == "test"
    assert Edit.from_dict({"adapter_name": "pro", "type": "test", "draft": "a", "final": "b"}).type == "test"
    with pytest.raises(ValueError):
        Conversation.from_dict({"adapter_name": "pro", "type": "valid"})


def test_source_is_optional():
    assert Conversation.from_dict({"adapter_name": "pro"}).source == ""
    assert Conversation.from_dict({"adapter_name": "pro", "source": "slack"}).source == "slack"
    assert Edit.from_dict({"adapter_name": "pro", "source": "discord", "draft": "a", "final": "b"}).context.source == "discord"


def test_prompt_allowed_without_conversation():
    c = Conversation.from_dict({"adapter_name": "pro", "prompt": "email my boss i'm sick"})
    assert c == conv(prompt="email my boss i'm sick", name="pro")


def test_masked_only_touches_me_and_echoes_earlier_them():
    c = conv(
        (THEM, "Venmo me $40? Lunch Friday at noon"),
        (ME, "Sent $40, Friday at noon works, call 310-555-1234"),
        (THEM, "my number is 310-555-1234 too lol"),
    )
    m = masked(c)
    assert m.messages[0] == c.messages[0]
    assert m.messages[1].text == "Sent $40, [DATE] at [TIME] works, call [PHONE]"
    assert m.messages[2] == c.messages[2]


def test_masked_keeps_request_facts():
    c = conv((ME, "dinner tuesday at 7pm?"), prompt="ask sarah to dinner tuesday")
    assert masked(c).messages[0].text == "dinner tuesday at [TIME]?"


def test_base_layout_and_loss_mask():
    tok = CharTokenizer()
    c = conv((THEM, "free tonight?"), (ME, "yes!"), (THEM, "8?"), (ME, "perfect"), (THEM, "cool"))
    [example], dropped = training_examples(tok, c, "base")
    assert dropped == 0
    assert tok.decode(example[0]) == f"Them: free tonight?\n\nMe: yes!{EOS}\n\nThem: 8?\n\nMe: perfect{EOS}"
    assert trained_text(tok, example) == f" yes!{EOS} perfect{EOS}"


def test_base_request_header():
    tok = CharTokenizer()
    c = conv((ME, "hey!"), prompt="say hi to sarah")
    [example], _ = training_examples(tok, c, "base")
    assert tok.decode(example[0]) == f"Request: say hi to sarah\n\nMe: hey!{EOS}"
    assert tok.decode(prompt_ids(tok, replace(c, messages=[]), "base")) == "Request: say hi to sarah\n\nMe:"


def test_source_header():
    tok = CharTokenizer()
    c = replace(conv((THEM, "lunch?"), (ME, "sure")), source="slack")
    [example], _ = training_examples(tok, c, "base")
    assert tok.decode(example[0]) == f"Source: slack\n\nThem: lunch?\n\nMe: sure{EOS}"
    assert trained_text(tok, example) == f" sure{EOS}"
    c = replace(conv(prompt="say hi to sarah"), source="gmail")
    assert tok.decode(prompt_ids(tok, c, "base")) == "Source: gmail\nRequest: say hi to sarah\n\nMe:"
    assert tok.decode(prompt_ids(tok, c, "instruct")).startswith(
        f"<|im_start|>system\n{SYSTEM_PROMPT}\n\nSource: gmail\nRequest: say hi to sarah<|im_end|>\n"
    )


def test_long_conversation_windows_train_each_me_message_once():
    tok = CharTokenizer()
    msgs = []
    for i in range(20):
        msgs += [(THEM, f"question number {i} " + "x" * 30), (ME, f"answer {i}")]
    examples, dropped = training_examples(tok, conv(*msgs), "base", max_len=120)
    assert dropped == 0 and len(examples) > 1
    assert all(len(t) <= 120 for t, _ in examples)
    assert "".join(trained_text(tok, e) for e in examples) == "".join(f" answer {i}{EOS}" for i in range(20))
    # Every window ends with the message it trains, right after the question it answers.
    for t, _ in examples:
        assert tok.decode(t).endswith(EOS)


def test_long_them_message_cut_from_start():
    tok = CharTokenizer()
    c = conv((THEM, "old " * 100 + "newest line"), (ME, "sure thing"))
    [(tokens, mask)], _ = training_examples(tok, c, "base", max_len=60)
    text = tok.decode(tokens)
    assert len(tokens) <= 60
    assert text.startswith("Them: …") and "newest line\n\nMe: sure thing" in text
    assert trained_text(tok, (tokens, mask)) == f" sure thing{EOS}"


def test_me_message_too_long_dropped():
    tok = CharTokenizer()
    examples, dropped = training_examples(tok, conv((THEM, "hi"), (ME, "x" * 200), (THEM, "?"), (ME, "ok")), "base", max_len=100)
    assert dropped == 1
    assert [trained_text(tok, e) for e in examples] == [f" ok{EOS}"]
    assert prompt_ids(tok, conv((THEM, "hi")), "base", budget=3) is None


def test_reply_example_matches_training_example():
    tok = CharTokenizer()
    context = conv((THEM, "free tonight?"))
    full = replace(context, messages=[*context.messages, Message(ME, "yes!")])
    assert reply_example(tok, context, "yes!", "base") == training_examples(tok, full, "base")[0][0]


def test_preference_example_fits_longer_completion():
    tok = CharTokenizer()
    e = Edit(context=conv((THEM, "t" * 300)), draft="a much longer draft " * 3, final="short")
    (chosen, cm), (rejected, rm) = preference_example(tok, e, "base", max_len=120)
    assert max(len(chosen), len(rejected)) <= 120
    prompt_len = cm.index(1)
    assert chosen[:prompt_len] == rejected[:prompt_len] and rm.index(1) == prompt_len
    assert tok.decode(chosen[prompt_len:]) == f" short{EOS}"


def test_cut_turn():
    assert cut_turn(" Sure, 10am.\nThem: Great!\n\nMe: np", "base") == "Sure, 10am."
    assert cut_turn("Me: what i said earlier is fine", "base") == "Me: what i said earlier is fine"
    assert cut_turn("Line one\nThem: kept", "instruct") == "Line one\nThem: kept"


def test_masked_edit_masks_final_not_draft():
    e = Edit(context=conv((THEM, "when works?")), draft="Tuesday works", final="Wednesday works")
    m = masked_edit(e)
    assert (m.draft, m.final, m.context) == ("Tuesday works", "[DATE] works", e.context)


MODELS = Path(__file__).parent.parent / "models"


@pytest.fixture(scope="module", params=[("base", "Qwen3-1.7B-Base"), ("instruct", "Qwen3-1.7B")])
def real(request):
    arm, name = request.param
    if not (MODELS / name / "tokenizer.json").exists():
        pytest.skip(f"models/{name} not downloaded")
    transformers = pytest.importorskip("transformers")
    return arm, transformers.AutoTokenizer.from_pretrained(MODELS / name)


def test_training_tokens_match_inference_prompts(real):
    """Every "me" message is trained after exactly the tokens the server would prompt with."""
    arm, tok = real
    c = conv((THEM, "hey are you free tonight?"), (ME, "yeah! what time"), (ME, "also where"), (THEM, "8, the usual"), (ME, "perfect"), prompt="")
    [(tokens, mask)], _ = training_examples(tok, c, arm)
    for k, m in enumerate(c.messages):
        if m.sender != ME:
            continue
        prompt = prompt_ids(tok, replace(c, messages=c.messages[:k]), arm)
        assert tokens[: len(prompt)] == prompt
        assert mask[len(prompt) - 1] == 0 and mask[len(prompt)] == 1


def test_instruct_layout():
    if not (MODELS / "Qwen3-1.7B" / "tokenizer.json").exists():
        pytest.skip("models/Qwen3-1.7B not downloaded")
    tok = pytest.importorskip("transformers").AutoTokenizer.from_pretrained(MODELS / "Qwen3-1.7B")
    c = conv((ME, "dinner tues?"), (THEM, "yes"), (ME, "great"), prompt="ask sarah to dinner")
    [(tokens, mask)], _ = training_examples(tok, c, "instruct")
    me = "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    assert tok.decode(tokens) == (
        f"<|im_start|>system\n{SYSTEM_PROMPT}\n\nRequest: ask sarah to dinner<|im_end|>\n"
        f"{me}dinner tues?<|im_end|>\n<|im_start|>user\nyes<|im_end|>\n{me}great<|im_end|>"
    )
    assert tok.decode([t for t, m in zip(tokens, mask) if m]) == "dinner tues?<|im_end|>great<|im_end|>"
