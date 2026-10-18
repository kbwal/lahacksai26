import mlx.core as mx

from train.augment import PERSONAS, persona_for, transcript


class WordTokenizer:
    """Each persona label is a few made-up ids; the chat template is a fixed prompt."""

    ids = {"professional": [1, 2], "social": [3, 4]}

    def encode(self, text, add_special_tokens=False):
        return self.ids[text]

    def apply_chat_template(self, messages, add_generation_prompt=True, enable_thinking=False):
        assert messages[-1]["role"] == "user"
        return [7, 8, 9]


def model_preferring(token):
    def model(x):
        logits = mx.zeros((1, x.shape[1], 10))
        return logits + (mx.arange(10) == token) * 5.0

    return model


def test_persona_is_the_higher_scoring_label():
    tok = WordTokenizer()
    assert persona_for(model_preferring(3), tok, "Me: hi") == "social"
    assert persona_for(model_preferring(1), tok, "Me: hi") == "professional"


def test_persona_ignores_tokens_that_are_not_labels():
    # The model's top token is neither label: still answers with one of them.
    assert persona_for(model_preferring(9), WordTokenizer(), "Me: hi") in PERSONAS


def test_transcript():
    d = {"conversation": [{"from": "them", "text": "dinner? "}, {"from": "me", "text": "yes!"}]}
    assert transcript(d) == "Them: dinner?\n\nMe: yes!"
    assert transcript(d, max_chars=5) == "Them:"


def test_transcript_of_request_only():
    assert transcript({"conversation": [], "prompt": "ask sarah to get dinner"}) == "Request: ask sarah to get dinner"
    assert transcript({"conversation": []}) == ""
