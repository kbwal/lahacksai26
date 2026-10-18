import plistlib
from types import SimpleNamespace

import pytest

from gmail_agent import service
from gmail_agent.__main__ import main, parser
from gmail_agent.locking import AlreadyRunning, process_lock


@pytest.fixture
def launchd(tmp_path, monkeypatch):
    target = tmp_path / "LaunchAgents" / f"{service.LABEL}.plist"
    monkeypatch.setattr(service, "agent_path", lambda: target)
    monkeypatch.setattr(service, "log_path", lambda: tmp_path / "Logs" / "gmail-worker.log")
    monkeypatch.setattr(service.sys, "platform", "darwin")
    state = {"loaded": False, "calls": []}

    def run(*args):
        state["calls"].append(args)
        if args[0] == "bootstrap":
            assert target.is_file()
            state["loaded"] = True
        elif args[0] == "bootout":
            state["loaded"] = False
        elif not state["loaded"]:
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
    result = service.install(args, "me@example.com")
    assert result["loaded"] and result["installed"]
    config = plistlib.loads(target.read_bytes())
    command = config["ProgramArguments"]
    assert command[:3] == [service.sys.executable, "-m", "gmail_agent"]
    assert command[command.index("--db") + 1] == str(args.db.resolve())
    assert command[command.index("--model") + 1] == str(args.model.resolve())
    assert command[command.index("--interval") + 1] == "1800"
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
        service.install(args, "other@example.com")
    assert target.read_bytes() == original


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
