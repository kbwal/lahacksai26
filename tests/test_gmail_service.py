import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from gmail_agent import service
from gmail_agent.__main__ import main, parser
from gmail_agent.locking import AlreadyRunning, process_lock, worker_lock


@pytest.fixture
def launchd(tmp_path, monkeypatch):
    monkeypatch.setattr(
        service, "agent_path", lambda label: tmp_path / "LaunchAgents" / f"{label}.plist"
    )
    monkeypatch.setattr(service, "log_path", lambda label: tmp_path / "Logs" / f"{label}.log")
    target = service.agent_path(service.job_label("me@example.com"))
    monkeypatch.setattr(service.sys, "platform", "darwin")
    state = {"loaded": set(), "calls": []}

    def run(*args):
        state["calls"].append(args)
        if args[0] == "bootstrap":
            target = Path(args[2])
            config = plistlib.loads(target.read_bytes())
            state["loaded"].add(config["Label"])
        elif args[0] == "bootout":
            state["loaded"].remove(args[1].split("/")[-1])
        elif args[1].split("/")[-1] not in state["loaded"]:
            return SimpleNamespace(returncode=113, stdout="", stderr="Could not find service")
        return SimpleNamespace(returncode=0, stdout="state = running\n pid = 42\n", stderr="")

    monkeypatch.setattr(service, "launchctl", run)
    model = tmp_path / "model with spaces"
    model.mkdir()
    (model / "config.json").write_text("{}")
    db = tmp_path / "private data" / "mail.db"
    args = parser().parse_args(["--db", str(db), "service", "install", "--model", str(model)])
    return target, state, args


def test_install_starts_private_login_job_with_absolute_arguments(launchd):
    target, state, args = launchd
    args.adapter_name = "professional"
    result = service.install(args, "me@example.com")
    assert result["loaded"] and result["installed"]
    config = plistlib.loads(target.read_bytes())
    command = config["ProgramArguments"]
    assert command[:3] == [service.sys.executable, "-m", "gmail_agent"]
    assert command[command.index("--db") + 1] == str(args.db.resolve())
    assert command[command.index("--model") + 1] == str(args.model.resolve())
    assert command[command.index("--interval") + 1] == "1800"
    assert command[command.index("--adapter-name") + 1] == "professional"
    assert config["RunAtLoad"] and config["KeepAlive"]
    assert config["EnvironmentVariables"]["HF_HUB_OFFLINE"] == "1"
    assert config["EnvironmentVariables"]["PYTHONPATH"]
    assert config["WorkingDirectory"] == str(service.Path.home())
    assert target.stat().st_mode & 0o777 == 0o600
    assert "refresh_token" not in target.read_text()
    assert any(call[0] == "bootstrap" for call in state["calls"])


def test_install_refuses_to_collide_with_unmanaged_worker(launchd):
    target, state, args = launchd
    with process_lock(args.db.with_suffix(".worker.lock")), pytest.raises(AlreadyRunning):
        service.install(args, "me@example.com")
    assert not target.exists()
    assert not state["loaded"]


def test_bundled_service_runs_executable_without_python_or_checkout(launchd, monkeypatch):
    target, _, args = launchd
    executable = "/Applications/Mail Companion.app/Contents/Helpers/worker/lahacks-mail-worker"
    monkeypatch.setattr(service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service.sys, "executable", executable)
    service.install(args, "me@example.com")
    config = plistlib.loads(target.read_bytes())
    assert config["ProgramArguments"][:2] == [executable, "--db"]
    assert "-m" not in config["ProgramArguments"]
    assert "PYTHONPATH" not in config["EnvironmentVariables"]


def test_stop_start_and_uninstall_preserve_mail_and_credentials(launchd):
    target, state, args = launchd
    service.install(args, "me@example.com")
    args.db.write_text("private mail")
    credential = args.db.parent / "credentials.json"
    credential.write_text("private credential")
    stopped = service.stop()
    assert stopped["installed"] and not stopped["loaded"]
    assert stopped["plist"] == str(target)
    assert service.start()["loaded"]
    assert not service.uninstall()["installed"]
    assert not state["loaded"]
    assert args.db.read_text() == "private mail"
    assert credential.read_text() == "private credential"
    assert not service.uninstall()["installed"]


def test_existing_service_is_not_overwritten(launchd):
    target, _, args = launchd
    service.install(args, "me@example.com")
    original = target.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        service.install(args, "ME@example.com")
    assert target.read_bytes() == original


def test_accounts_have_independent_services_and_controls(launchd):
    target, state, args = launchd
    first = service.install(args, "me@example.com")
    # A running worker in the same DB must not block another account's service.
    with worker_lock(args.db, "me@example.com"):
        second = service.install(args, "other@example.com")
    assert first["label"] != second["label"]
    assert first["log"] != second["log"]
    assert len(state["loaded"]) == 2
    with pytest.raises(ValueError, match="--account"):
        service.status()
    assert not service.stop("me@example.com")["loaded"]
    assert service.status("other@example.com")["loaded"]
    assert service.start("me@example.com")["loaded"]
    assert not service.uninstall("other@example.com")["installed"]
    assert target.exists()
    assert service.status()["loaded"]


def test_legacy_service_remains_controllable_by_account(launchd):
    _, state, args = launchd
    target = service.agent_path(service.LABEL)
    target.parent.mkdir(parents=True)
    target.write_bytes(
        plistlib.dumps(
            {
                "Label": service.LABEL,
                "ProgramArguments": ["python", "--account", "me@example.com", "run"],
                "StandardOutPath": "/legacy/log",
            }
        )
    )
    state["loaded"].add(service.LABEL)
    assert service.status("me@example.com")["label"] == service.LABEL
    with pytest.raises(ValueError, match="already exists"):
        service.install(args, "me@example.com")
    assert not service.uninstall("me@example.com")["installed"]
    assert not state["loaded"]


def test_cli_targets_only_requested_service(launchd, capsys):
    _, _, args = launchd
    service.install(args, "me@example.com")
    service.install(args, "other@example.com")
    assert main(["--account", "other@example.com", "service", "stop"]) == 0
    assert '"loaded": false' in capsys.readouterr().out
    assert service.status("me@example.com")["loaded"]


def test_unknown_launchctl_failure_is_not_reported_as_missing(launchd, monkeypatch):
    monkeypatch.setattr(
        service,
        "launchctl",
        lambda *_: SimpleNamespace(returncode=1, stdout="", stderr="Operation not permitted"),
    )
    with pytest.raises(ValueError, match="Cannot inspect"):
        service.status()


def test_service_status_does_not_require_a_connected_account(launchd, tmp_path, capsys):
    assert main(["--db", str(tmp_path / "empty.db"), "service", "status"]) == 0
    assert '"loaded": false' in capsys.readouterr().out
    assert not (tmp_path / "empty.db").exists()
