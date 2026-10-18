import json
from pathlib import Path

import pytest

from gmail_agent import auth, runtime
from gmail_agent.__main__ import main, parser
from gmail_agent.store import Store


def test_bundled_defaults_are_outside_installation_and_working_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("LAHACKS_MAIL_HOME", raising=False)
    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir("/tmp")
    args = parser().parse_args(["run"])
    home = tmp_path / "Library/Application Support/lahacksai26"
    assert args.db == home / "data/mail.db"
    assert args.model == home / "models/Qwen3-4B-Base"
    assert args.model_lock == home / "data/model.lock"
    assert not home.exists()


def test_app_can_select_an_isolated_home_and_override_individual_paths(tmp_path, monkeypatch):
    home = tmp_path / "app data"
    monkeypatch.setenv("LAHACKS_MAIL_HOME", str(home))
    args = parser().parse_args(["--db", str(tmp_path / "existing.db"), "run"])
    assert args.db == tmp_path / "existing.db"
    assert args.model == home / "models/Qwen3-4B-Base"
    assert args.model_lock == home / "data/model.lock"
    monkeypatch.setenv("LAHACKS_MAIL_HOME", "relative")
    with pytest.raises(SystemExit) as error:
        parser().parse_args(["info"])
    assert error.value.code == 2


def test_development_and_frozen_processes_share_default_model_lock(tmp_path, monkeypatch):
    from gmail_agent.replies import LocalGenerator

    monkeypatch.delenv("LAHACKS_MAIL_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    monkeypatch.chdir(tmp_path)
    development = parser().parse_args(["run"])
    assert LocalGenerator("model").model_lock == development.model_lock
    monkeypatch.setattr(runtime.sys, "frozen", True)
    monkeypatch.chdir("/tmp")
    bundled = parser().parse_args(["run"])
    assert development.model_lock == bundled.model_lock


def test_info_is_json_and_never_opens_a_database_or_keychain(tmp_path, monkeypatch, capsys):
    home = tmp_path / "not created"
    monkeypatch.setenv("LAHACKS_MAIL_HOME", str(home))
    monkeypatch.setattr(auth, "keychain", lambda: pytest.fail("Keychain was accessed"))
    assert main(["info"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["interface_version"] == 1
    assert result["paths"]["db"] == str(home / "data/mail.db")
    assert not home.exists()


def test_connect_keeps_library_instructions_off_json_stdout(tmp_path, monkeypatch, capsys):
    def connect(_, **_kwargs):
        print("Opening Google sign-in")
        return "me@example.com"

    monkeypatch.setattr(auth, "connect", connect)
    assert (
        main(["--db", str(tmp_path / "mail.db"), "connect", "--client-secrets", "/not-read.json"])
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"account": "me@example.com", "connected": True}
    assert "Opening Google" in captured.err


def test_self_check_has_no_mailbox_side_effects(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LAHACKS_MAIL_HOME", str(tmp_path / "untouched"))

    def check(args):
        assert args.model is None  # Loading a model is explicitly requested.
        print("Library diagnostic")
        return {"ok": True, "inference_checked": False}

    monkeypatch.setattr(runtime, "self_check", check)
    assert main(["self-check"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"]
    assert "Library diagnostic" in captured.err
    assert not (tmp_path / "untouched").exists()


def test_model_warnings_do_not_corrupt_once_json(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from gmail_agent import worker

    path = tmp_path / "mail.db"
    store = Store(path)
    with store.db:
        store.add_account("me@example.com", "reference", 100)
    store.close()
    monkeypatch.setattr(
        auth, "client_for", lambda _: SimpleNamespace(session=SimpleNamespace(close=lambda: None))
    )

    def run(*_args, **_kwargs):
        print("Model memory warning")
        return {"published": 0}

    monkeypatch.setattr(worker, "run_locked", run)
    assert main(["--db", str(path), "once"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"published": 0}
    assert "Model memory warning" in captured.err


def test_self_check_failure_reports_missing_library_without_opening_mail(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("LAHACKS_MAIL_HOME", str(tmp_path / "untouched"))

    def check(_args):
        raise ImportError("missing native helper")

    monkeypatch.setattr(runtime, "self_check", check)
    assert main(["self-check"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "ImportError: missing native helper" in captured.err
    assert not (tmp_path / "untouched").exists()
