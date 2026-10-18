"""Verify the shipped archive from an unrelated directory with an isolated data home."""

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--model", type=Path, help="Also generate a synthetic reply locally")
    parser.add_argument("--arm", choices=("instruct", "base"), help="Default: from the adapter or model")
    parser.add_argument("--adapter-path", type=Path, help="Also load this trained LoRA adapter")
    parser.add_argument("--model-lock", type=Path, help="Share the running worker's model lock")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lahacks-sidecar-") as temporary:
        root = Path(temporary).resolve()
        subprocess.run(
            ["/usr/bin/ditto", "-x", "-k", str(args.archive.resolve()), str(root)], check=True
        )
        bundle = root / "lahacks-mail-worker"
        executable = bundle / "lahacks-mail-worker"
        forbidden = [
            path
            for path in bundle.rglob("*")
            if path.suffix in {".db", ".mbox", ".safetensors"}
            or path.name.startswith("client_secret")
            or path.name in {"credentials.json", ".git", ".venv"}
        ]
        if forbidden:
            raise RuntimeError(
                "Archive contains private data, model weights, or source environment"
            )
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(executable)], check=True)
        app_home = root / "Application Support" / "test app"
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
        }
        env.update(LAHACKS_MAIL_HOME=str(app_home), PATH="/usr/bin:/bin")

        def call(*arguments):
            result = subprocess.run(
                [str(executable), *arguments],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
                check=True,
            )
            return json.loads(result.stdout)

        info = call("info")
        assert info["bundled"] and info["interface_version"] == 1
        assert info["paths"]["db"] == str(app_home / "data/mail.db")
        assert not app_home.exists()
        assert call("status") == []
        assert (app_home / "data/mail.db").stat().st_mode & 0o777 == 0o600
        # Exercise the additional command namespace from the relocated executable.
        # The fixture is synthetic and never touches a provider or Keychain token.
        with sqlite3.connect(app_home / "data/mail.db") as db:
            db.execute(
                "INSERT INTO accounts(account_id,email,credential_ref,activated_at) VALUES (?,?,?,?)",
                ("demo@example.com", "demo@example.com", "synthetic", 0),
            )
            db.execute(
                "INSERT INTO threads(account_id,thread_id) VALUES (?,?)",
                ("demo@example.com", "test-thread"),
            )
            db.execute(
                """INSERT INTO messages(account_id,message_id,thread_id,internal_date,labels,headers,body_text)
                   VALUES (?,?,?,?,?,?,?)""",
                ("demo@example.com", "test-message", "test-thread", 1,
                 '["SENT"]', '{"from":"demo@example.com"}', "Synthetic reply"),
            )
        export = app_home / "data/messages.jsonl"
        exported = call("messages", "--source", "gmail", "--account", "demo@example.com",
                        "export", "--output", str(export))
        assert exported["messages"] == 1
        assert json.loads(export.read_text())["source"] == "gmail"
        arguments = ["self-check"]
        if args.model:
            arguments.extend(["--model", str(args.model.resolve())])
            if args.arm:
                arguments.extend(["--arm", args.arm])
            if args.adapter_path:
                arguments.extend(["--adapter-path", str(args.adapter_path.resolve())])
        if args.model_lock:
            arguments.extend(["--model-lock", str(args.model_lock.resolve())])
        checked = call(*arguments)
        assert checked["ok"] and checked["metal"]
        assert checked["inference_checked"] == bool(args.model)
        print(json.dumps({"relocation": True, "private_data_excluded": True, **checked}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        # These commands use only synthetic input and an empty temporary mailbox.
        raise SystemExit(f"Bundle check failed: {error.stderr or error}") from error
