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
    request = [payload["prompt"].strip()]
    if payload["recipient"].strip():
        request.append(f"Recipient: {payload['recipient'].strip()}")
    if payload["subject"].strip():
        request.append(f"Subject: {payload['subject'].strip()}")
    return {
        "source": payload["appId"] if payload["appId"] in SOURCES else "",
        "prompt": "\n".join(request),
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


def compare(payload):
    """The same request from the plain model and then each persona's adapter.

    The model loads once. The first adapter is attached with load_adapters; the
    others share its keys and rank, so their weights are swapped into the same
    LoRA layers instead of reloading the model."""
    context = compose_context(payload)
    model_path = Path(payload.get("modelPath", "")).expanduser()
    if not model_path.is_absolute() or not (model_path / "config.json").is_file():
        raise ValueError("Choose a local model folder containing config.json")
    persona_ids = payload.get("personaIds")
    if not isinstance(persona_ids, list) or not 1 <= len(persona_ids) <= 4 or not all(
        isinstance(p, str) and re.fullmatch(r"[\w-]{1,100}", p) for p in persona_ids
    ):
        raise ValueError("Invalid personaIds")
    import gc

    import mlx.core as mx
    from mlx_lm import load
    from mlx_lm.tuner.utils import load_adapters

    from gmail_agent.locking import process_lock
    from gmail_agent.replies import adapter_config, check_base_model, resolve_arm
    from gmail_agent.training import generation_input
    from train.draft import draft

    root = default_paths()["adapters"]
    results = []
    with process_lock(default_paths()["model_lock"]):
        model, tokenizer = load(str(model_path))
        try:
            arm = resolve_arm(model_path, {}, None)
            results.append({"id": None, "body": draft(model, tokenizer, generation_input(context, "base"), arm, 200).strip()})
            attached = False
            for persona_id in persona_ids:
                adapter = root / persona_id
                if not (adapter / "adapters.safetensors").is_file():
                    results.append({"id": persona_id, "body": "", "missing": True})
                    continue
                config = adapter_config(adapter)
                check_base_model(model_path, config)
                if attached:
                    model.load_weights(str(adapter / "adapters.safetensors"), strict=False)
                else:
                    load_adapters(model, str(adapter))
                    attached = True
                arm = resolve_arm(model_path, config, None)
                body = draft(model, tokenizer, generation_input(context, persona_id), arm, 200).strip()
                results.append({"id": persona_id, "body": body})
        finally:
            del model
            gc.collect()
            mx.clear_cache()
    return {"results": results}
