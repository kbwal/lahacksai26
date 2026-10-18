"""Manage a per-account macOS LaunchAgent for the Gmail worker."""

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

from gmail_agent import runtime
from gmail_agent.locking import account_key, worker_lock

LABEL = "com.lahacksai26.gmail-worker"


def agent_path(label):
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def log_path(label):
    return Path.home() / "Library" / "Logs" / "lahacksai26" / f"{label}.log"


def job_label(account_id=None):
    legacy = agent_path(LABEL)
    if account_id:
        account_id = account_id.lower()
        # Existing installations remain controllable until explicitly reinstalled.
        if legacy.exists():
            with legacy.open("rb") as source:
                command = plistlib.load(source)["ProgramArguments"]
            if (
                "--account" in command
                and command[command.index("--account") + 1].lower() == account_id
            ):
                return LABEL
        return f"{LABEL}.{account_key(account_id)}"
    installed = list(legacy.parent.glob(f"{LABEL}.*.plist"))
    if legacy.exists():
        installed.append(legacy)
    if len(installed) > 1:
        raise ValueError("Use --account to select a login service")
    return installed[0].stem if installed else LABEL


def launchctl(*arguments):
    return subprocess.run(
        ["/bin/launchctl", *arguments], capture_output=True, text=True, timeout=30, check=False
    )


def domain():
    if sys.platform != "darwin":
        raise ValueError("The login service requires macOS")
    return f"gui/{os.getuid()}"


def status(account_id=None):
    label = job_label(account_id)
    target = agent_path(label)
    result = launchctl("print", f"{domain()}/{label}")
    if result.returncode and "Could not find service" not in result.stderr:
        raise ValueError(f"Cannot inspect the Gmail login service: {result.stderr.strip()}")
    fields = {"label": label, "installed": target.exists(), "loaded": result.returncode == 0}
    for name in ("state", "pid", "last exit code"):
        match = re.search(rf"^\s*{name} = (.+)$", result.stdout, re.MULTILINE)
        if match:
            fields[name.replace(" ", "_")] = match[1].strip()
    fields["plist"] = str(target)
    if fields["installed"]:
        with target.open("rb") as source:
            fields["log"] = plistlib.load(source)["StandardOutPath"]
    return fields


def start(account_id=None):
    current = status(account_id)
    if not current["installed"]:
        raise ValueError("The login service is not installed; run service install first")
    if not current["loaded"]:
        result = launchctl("bootstrap", domain(), current["plist"])
        if result.returncode:
            raise ValueError(f"Cannot start the Gmail login service: {result.stderr.strip()}")
    return status(account_id)


def stop(account_id=None):
    current = status(account_id)
    if current["loaded"]:
        result = launchctl("bootout", f"{domain()}/{current['label']}")
        if result.returncode:
            raise ValueError(f"Cannot stop the Gmail login service: {result.stderr.strip()}")
    return status(account_id)


def uninstall(account_id=None):
    current = stop(account_id)
    Path(current["plist"]).unlink(missing_ok=True)
    return status(account_id)


def install(args, account_id):
    account_id = account_id.lower()
    current = status(account_id)
    if current["installed"] or current["loaded"]:
        raise ValueError("A Gmail login service already exists; uninstall it before reconfiguring")
    root = Path(__file__).resolve().parent.parent
    model = args.model.resolve()
    if not (model / "config.json").is_file():
        raise ValueError("Local model path is missing config.json")
    if args.adapter_path and not args.adapter_path.is_dir():
        raise ValueError("Adapter directory does not exist")
    db = args.db.resolve()
    command = [sys.executable]
    if not runtime.frozen():
        command.extend(["-m", "gmail_agent"])
    command.extend(
        [
            "--db",
            str(db),
            "--account",
            account_id,
            "run",
            "--model",
            str(model),
            "--arm",
            args.arm,
            "--max-tokens",
            str(args.max_tokens),
            "--draft-limit",
            str(args.draft_limit),
            "--model-lock",
            str(args.model_lock.resolve()),
            "--interval",
            str(args.interval),
        ]
    )
    if args.adapter_path:
        command.extend(["--adapter-path", str(args.adapter_path.resolve())])
    if args.adapter_name:
        command.extend(["--adapter-name", args.adapter_name])
    label = current["label"]
    log_file = log_path(label)
    environment = {
        "PYTHONUNBUFFERED": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    if not runtime.frozen():
        environment["PYTHONPATH"] = str(root)
    with worker_lock(db, account_id):
        # Check before installing so launchd cannot loop on an unmanaged worker's lock.
        log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_file.touch(mode=0o600, exist_ok=True)
        log_file.chmod(0o600)
        target = agent_path(label)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as output:
            target.chmod(0o600)
            plistlib.dump(
                {
                    "Label": label,
                    "ProgramArguments": command,
                    # launchd can fail before exec when its working directory is on Desktop.
                    "WorkingDirectory": str(Path.home()),
                    "RunAtLoad": True,
                    "KeepAlive": True,
                    "ThrottleInterval": 30,
                    "ExitTimeOut": 60,
                    "Umask": 0o077,
                    "EnvironmentVariables": environment,
                    "StandardOutPath": str(log_file),
                    "StandardErrorPath": str(log_file),
                },
                output,
            )
    # Release the lock before launchd starts the actual worker.
    return start(account_id)
