"""Paths and diagnostics for the desktop application's bundled worker."""

import os
import platform
import sys
from pathlib import Path

from gmail_agent import __version__


def frozen():
    return getattr(sys, "frozen", False)


def default_paths():
    app_home = Path.home() / "Library" / "Application Support" / "lahacksai26"
    configured = os.environ.get("LAHACKS_MAIL_HOME")
    if configured:
        home = Path(configured).expanduser()
        if not home.is_absolute():
            raise ValueError("LAHACKS_MAIL_HOME must be an absolute path")
    elif frozen():
        home = app_home
    else:
        # Preserve the existing development checkout and installed login service.
        home = Path(".")
    return {
        "home": home,
        "db": home / "data" / "mail.db",
        "model": home / "models" / "Qwen3.5-2B",
        # Development and bundled processes share Metal even with different cwd's.
        "model_lock": (home if configured else app_home) / "data" / "model.lock",
    }


def info():
    return {
        "version": __version__,
        "interface_version": 1,
        "bundled": bool(frozen()),
        "platform": sys.platform,
        "architecture": platform.machine(),
        "paths": {name: str(path.resolve()) for name, path in default_paths().items()},
    }


def self_check(args):
    """Exercise bundled libraries and optionally inference, without touching Gmail."""
    import sqlite3

    import mlx.core as mx
    import requests

    from gmail_agent.auth import keychain

    if not mx.metal.is_available():
        raise RuntimeError("Metal is unavailable")
    with mx.stream(mx.gpu):
        total = mx.sum(mx.array([1.0, 2.0]))
        mx.eval(total)
    if total.item() != 3.0 or not Path(requests.certs.where()).is_file():
        raise RuntimeError("Runtime self-check failed")
    with sqlite3.connect(":memory:") as db:
        db.execute("SELECT 1").fetchone()
    result = {
        "ok": True,
        "metal": True,
        "keychain_backend": type(keychain()).__module__,
        "inference_checked": False,
    }
    if args.model:
        from gmail_agent.replies import LocalGenerator

        generator = LocalGenerator(
            args.model,
            adapter_path=args.adapter_path,
            adapter_name=args.adapter_name,
            arm=args.arm,
            max_tokens=args.max_tokens,
            model_lock=args.model_lock,
        )
        try:
            output = generator.generate(
                {
                    "recipient": "teammate@example.com",
                    "relationship": "teammate",
                    "thread": "Please confirm that you received the project notes.",
                    "reply": "",
                    "conversation": [
                        {
                            "from": "them",
                            "text": "Please confirm that you received the project notes.",
                        }
                    ],
                }
            )
            if not output.strip():
                raise RuntimeError("Model returned no text")
            result.update(inference_checked=True, model_version=generator.version)
        finally:
            generator.close()
    return result
