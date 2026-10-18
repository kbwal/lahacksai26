"""Generate a new message locally without accessing or sending mail."""

from pathlib import Path

from gmail_agent.locking import process_lock
from gmail_agent.runtime import default_paths


def compose_messages(payload):
    limits = {"prompt": 6000, "persona": 50, "description": 300, "recipient": 320, "subject": 300}
    for key, limit in limits.items():
        value = payload.get(key)
        if not isinstance(value, str) or len(value) > limit or "\0" in value:
            raise ValueError(f"Invalid {key}")
    if not payload["prompt"].strip() or not payload["persona"].strip():
        raise ValueError("Choose a persona and describe your message")
    if payload.get("appId") not in {"gmail", "slack", "outlook"}:
        raise ValueError("Choose a valid app")
    return [
        {
            "role": "system",
            "content": (
                "Write a NEW outgoing message, not a reply. Return only the message body. "
                "Use the requested persona's style. Do not claim to know the user's writing history. "
                "Preserve facts provided in the request. Use [DATE], [TIME], [AMOUNT], [PHONE], "
                "[ADDRESS] or [LINK] for unknown or changeable facts; never invent them.\n"
                f"Persona: {payload['persona']}\nStyle: {payload['description']}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"App: {payload['appId']}\nTo: {payload['recipient']}\n"
                f"Subject: {payload['subject']}\nRequest:\n{payload['prompt']}"
            ),
        },
    ]


def compose(payload):
    messages = compose_messages(payload)
    model_path = Path(payload.get("modelPath", "")).expanduser()
    if not model_path.is_absolute() or not (model_path / "config.json").is_file():
        raise ValueError("Choose a local instruct model folder containing config.json")
    with process_lock(default_paths()["model_lock"]):
        from mlx_lm import generate, load

        model, tokenizer = load(str(model_path))
        prompt = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, enable_thinking=False, tokenize=False
        )
        body = generate(model, tokenizer, prompt=prompt, max_tokens=700, verbose=False).strip()
    if not body or len(body) > 60000:
        raise ValueError("Model returned an empty or oversized message. Try a shorter prompt.")
    return {"body": body}
