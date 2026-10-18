"""Generate a new message locally without accessing or sending mail."""

import re
from pathlib import Path

from gmail_agent.runtime import default_paths
from message_pipeline import SOURCES


def compose_context(payload):
    """The request as train/augment.py teaches it: a new "me" message from `prompt`, on
    the app's channel, written by the persona's adapter."""
    limits = {"prompt": 6000, "persona": 50, "description": 300, "recipient": 320, "subject": 300}
    for key, limit in limits.items():
        value = payload.get(key)
        if not isinstance(value, str) or len(value) > limit or "\0" in value:
            raise ValueError(f"Invalid {key}")
    if not payload["prompt"].strip() or not payload["persona"].strip():
        raise ValueError("Choose a persona and describe your message")
    if payload.get("appId") not in {"gmail", "slack", "discord", "outlook"}:
        raise ValueError("Choose a valid app")
    persona_id = payload.get("personaId")
    if not isinstance(persona_id, str) or not re.fullmatch(r"[\w-]{1,100}", persona_id):
        raise ValueError("Invalid personaId")
    return {
        "source": payload["appId"] if payload["appId"] in SOURCES else "",
        "prompt": payload["prompt"].strip(),
        "conversation": [],
    }


def compose(payload):
    context = compose_context(payload)
    model_path = Path(payload.get("modelPath", "")).expanduser()
    if not model_path.is_absolute() or not (model_path / "config.json").is_file():
        raise ValueError("Choose a local model folder containing config.json")
    from gmail_agent.replies import LocalGenerator

    generator = LocalGenerator(
        model_path,
        adapter_root=default_paths()["adapters"],
        adapter_name=payload["personaId"],
        max_tokens=700,
    )
    try:
        body = generator.generate(context).strip()
    finally:
        generator.close()
    if not body or len(body) > 60000:
        raise ValueError("Model returned an empty or oversized message. Try a shorter prompt.")
    return {"body": body}
