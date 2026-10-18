import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.auth.exceptions import TransportError

from gmail_agent import auth
from gmail_agent.__main__ import main, parser
from gmail_agent.store import Store


def test_connect_uses_pkce_loopback_and_stores_only_in_keychain(tmp_path, monkeypatch, caplog):
    client_json = tmp_path / "client.json"
    client_json.write_text(json.dumps({"installed": {"client_id": "test"}}))
    calls = {}
    credentials = SimpleNamespace(
        refresh_token="secret-refresh",
        scopes=auth.SCOPES,
        granted_scopes=auth.SCOPES,
        to_json=lambda: '{"refresh_token":"secret-refresh"}',
    )

    class Flow:
        def run_local_server(self, **kwargs):
            calls["server"] = kwargs
            logging.getLogger("google_auth_oauthlib.flow").info("GET /?code=secret-code")
            return credentials

    def make_flow(config, scopes, **kwargs):
        calls["flow"] = (config, scopes, kwargs)
        return Flow()

    monkeypatch.setattr(auth.InstalledAppFlow, "from_client_config", make_flow)
    monkeypatch.setattr(auth, "AuthorizedSession", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        auth,
        "GmailClient",
        lambda _: SimpleNamespace(profile=lambda: {"emailAddress": "Me@Example.com"}),
    )
    monkeypatch.setattr(
        auth,
        "keychain",
        lambda: SimpleNamespace(set_password=lambda *args: calls.setdefault("keychain", args)),
    )
    with caplog.at_level(logging.INFO, logger="google_auth_oauthlib.flow"):
        assert auth.connect(client_json) == "me@example.com"
    assert "secret-code" not in caplog.text
    assert calls["flow"][2]["autogenerate_code_verifier"] is True
    assert calls["server"]["host"] == "127.0.0.1"
    assert calls["server"]["port"] == 0
    assert calls["server"]["access_type"] == "offline"
    assert calls["keychain"][:2] == (auth.SERVICE, "me@example.com")
    assert list(tmp_path.iterdir()) == [client_json]


def test_web_oauth_client_is_rejected(tmp_path):
    client_json = tmp_path / "client.json"
    client_json.write_text('{"web": {}}')
    with pytest.raises(ValueError, match="Desktop"):
        auth.connect(client_json)


@pytest.mark.parametrize(
    ("refresh_token", "scopes"),
    [(None, auth.SCOPES), ("refresh", auth.SCOPES[:1])],
)
def test_missing_offline_access_or_partial_consent_is_rejected(
    tmp_path, monkeypatch, refresh_token, scopes
):
    client_json = tmp_path / "client.json"
    client_json.write_text('{"installed": {}}')
    credentials = SimpleNamespace(
        refresh_token=refresh_token, granted_scopes=scopes, scopes=auth.SCOPES
    )
    monkeypatch.setattr(
        auth.InstalledAppFlow,
        "from_client_config",
        lambda *_args, **_kwargs: SimpleNamespace(run_local_server=lambda **_kwargs: credentials),
    )
    with pytest.raises(RuntimeError, match="offline access"):
        auth.connect(client_json)


def test_client_with_expired_token_can_start_offline(monkeypatch):
    saved = json.dumps(
        {
            "token": "expired-access",
            "expiry": "2020-01-01T00:00:00Z",
            "refresh_token": "test-refresh",
            "client_id": "test-client",
            "client_secret": "test-secret",
        }
    )
    monkeypatch.setattr(auth, "keychain", lambda: SimpleNamespace(get_password=lambda *_: saved))

    def no_startup_network(*_):
        pytest.fail("Credential refresh ran before entering the worker retry loop")

    monkeypatch.setattr(auth.Credentials, "refresh", no_startup_network)
    client = auth.client_for("me@example.com")
    try:
        assert not client.session.credentials.valid
        assert client.session.credentials.refresh_token == "test-refresh"
    finally:
        client.session.close()


def test_request_time_refresh_retries_and_persists_rotated_credentials(monkeypatch):
    saved = json.dumps(
        {
            "refresh_token": "old-refresh",
            "client_id": "test-client",
            "client_secret": "test-secret",
        }
    )
    writes = []
    monkeypatch.setattr(
        auth,
        "keychain",
        lambda: SimpleNamespace(
            get_password=lambda *_: saved,
            set_password=lambda *args: writes.append(args),
        ),
    )
    client = auth.client_for("me@example.com")
    credentials = client.session.credentials

    def offline(**_kwargs):
        raise TransportError("offline")

    def online(**_kwargs):
        return SimpleNamespace(
            status=200,
            data=json.dumps(
                {
                    "access_token": "new-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            ).encode(),
        )

    try:
        with pytest.raises(TransportError):
            credentials.before_request(offline, "GET", "https://gmail.googleapis.com/", {})
        assert not writes
        headers = {}
        credentials.before_request(online, "GET", "https://gmail.googleapis.com/", headers)
        assert headers["authorization"] == "Bearer new-access"
        assert len(writes) == 1
        assert writes[0][:2] == (auth.SERVICE, "me@example.com")
        assert json.loads(writes[0][2])["refresh_token"] == "rotated-refresh"
        credentials.before_request(offline, "GET", "https://gmail.googleapis.com/", {})
        assert len(writes) == 1  # A valid access token needs no network refresh.
    finally:
        client.session.close()


def test_run_defaults_and_positive_limits():
    args = parser().parse_args(["run"])
    assert args.interval == 1800
    assert args.model == Path("models/Qwen3.5-2B")
    assert args.arm == "instruct"
    with pytest.raises(SystemExit):
        parser().parse_args(["run", "--interval", "0"])


def test_status_requires_no_oauth_and_does_not_show_bodies(tmp_path, capsys):
    path = tmp_path / "mail.db"
    store = Store(path)
    with store.db:
        store.add_account("me@example.com", "reference", 100)
        store.ensure_thread("me@example.com", "thread")
        store.enqueue("me@example.com", "thread", "trigger", {"thread": "private email body"})
    store.close()
    assert main(["--db", str(path), "status"]) == 0
    output = capsys.readouterr().out
    assert "private email body" not in output
    assert "reference" not in output
    assert json.loads(output)[0]["drafts"]["queued"] == 1


def test_feedback_export_writes_private_file_and_does_not_overwrite(tmp_path):
    path = tmp_path / "mail.db"
    store = Store(path)
    with store.db:
        store.add_account("me@example.com", "reference", 100)
    store.close()
    target = tmp_path / "feedback.jsonl"
    assert main(["--db", str(path), "export-feedback", "--output", str(target)]) == 0
    assert target.stat().st_mode & 0o777 == 0o600
    target.write_text("keep me")
    assert main(["--db", str(path), "export-feedback", "--output", str(target)]) == 1
    assert target.read_text() == "keep me"
