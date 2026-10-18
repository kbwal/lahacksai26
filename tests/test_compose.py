import pytest

from gmail_agent.compose import compose_messages


def payload():
    return {
        "prompt": "Invite Alex to the demo. Use [LINK].",
        "persona": "Professional",
        "description": "Clear and concise",
        "recipient": "Alex",
        "subject": "Demo",
        "appId": "gmail",
    }


def test_new_message_prompt_carries_persona_and_facts():
    messages = compose_messages(payload())
    assert "NEW outgoing message" in messages[0]["content"]
    assert "Professional" in messages[0]["content"]
    assert "Clear and concise" in messages[0]["content"]
    assert "Invite Alex" in messages[1]["content"]
    assert "[LINK]" in messages[1]["content"]


@pytest.mark.parametrize(
    "field,value", [("prompt", ""), ("prompt", "x" * 6001), ("appId", "invalid"), ("persona", None)]
)
def test_invalid_request_rejected_before_model_load(field, value):
    data = payload()
    data[field] = value
    with pytest.raises(ValueError):
        compose_messages(data)
