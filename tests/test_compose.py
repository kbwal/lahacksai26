import json

import pytest

from gmail_agent.compose import compose_context
from gmail_agent.replies import LocalGenerator
from gmail_agent.training import generation_input


def payload():
    return {
        "prompt": "Invite Alex to the demo. Use [LINK].",
        "persona": "Professional",
        "personaId": "professional",
        "description": "Clear and concise",
        "recipient": "Alex",
        "subject": "Demo",
        "appId": "gmail",
    }


@pytest.mark.parametrize("app,source", [("gmail", "gmail"), ("slack", "slack"), ("outlook", "")])
def test_new_message_is_a_training_format_request(app, source):
    context = compose_context({**payload(), "appId": app})
    conversation = generation_input(context, "professional")
    assert conversation.prompt == "Invite Alex to the demo. Use [LINK]."
    assert conversation.messages == []
    assert conversation.source == source


def test_composer_loads_the_personas_trained_adapter(tmp_path):
    adapter = tmp_path / "adapters" / "social"
    adapter.mkdir(parents=True)
    (adapter / "adapters.safetensors").write_bytes(b"")
    (adapter / "adapter_config.json").write_text(json.dumps({"arm": "base"}))
    generator = LocalGenerator(tmp_path, adapter_root=tmp_path / "adapters", adapter_name="social")
    assert generator.adapter_path == adapter.resolve()
    assert generator.arm == "base"


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt", ""),
        ("prompt", "x" * 6001),
        ("appId", "invalid"),
        ("persona", None),
        ("personaId", "../other"),
        ("personaId", None),
    ],
)
def test_invalid_request_rejected_before_model_load(field, value):
    data = payload()
    data[field] = value
    with pytest.raises(ValueError):
        compose_context(data)
