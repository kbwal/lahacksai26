import json
import runpy
import sys
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from gmail_agent.__main__ import main as worker_main
from gmail_agent.store import Store as GmailStore
from message_pipeline.__main__ import main, write_jsonl
from message_pipeline.clients import DiscordClient, ProviderError, RateLimited, SlackClient
from message_pipeline.models import Message
from message_pipeline.replies import dismiss, generate_suggestions, reconcile
from message_pipeline.store import Store, export_gmail
from message_pipeline.sync import sync_once
from train.format import Pair, prompt_text

KEY = ("slack", "U1", "T1")


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "messages.db")
    result.connect(KEY, "test-key", ["C1"])
    yield result
    result.close()


def message(**kwargs):
    defaults = {
        "source": "slack",
        "account_id": "U1",
        "workspace_id": "T1",
        "channel_id": "C1",
        "thread_id": "1700000000.000001",
        "message_id": "1700000000.000002",
        "author_id": "U1",
        "timestamp_ms": 1700000000000,
        "text": "A human reply",
        "is_self": True,
    }
    return Message(**{**defaults, **kwargs})


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, (dict, list)):
            return SimpleNamespace(status_code=200, headers={}, json=lambda: response)
        return response

    def close(self):
        pass


class Client:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.states = []

    def identity(self):
        return "U1", "T1"

    def validate_channel(self, *_):
        pass

    def initial_state(self):
        return {"page": 1}

    def page(self, account, workspace, channel, state):
        self.states.append(state)
        page = next(self.pages)
        if isinstance(page, Exception):
            raise page
        return page


def test_source_and_workspace_ids_do_not_collide(store):
    for source, account, workspace in [KEY, ("discord", "U1", "T1"), ("slack", "U1", "T2")]:
        key = (source, account, workspace)
        store.connect(key, "test", ["C1"])
        store.save_page(key, "C1", 1, [message(source=source, workspace_id=workspace)], None)
        assert next(iter(store.export(key)))["source"] == source
    assert store.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
    with pytest.raises(ValueError, match="escaped"):
        store.save_page(KEY, "C1", 2, [message(), message(workspace_id="T2")], None)
    assert store.db.execute("SELECT max(generation) FROM messages").fetchone()[0] == 1


def test_resume_failure_and_replay_are_idempotent(store):
    client = Client([([message()], {"page": 2}), OSError("private payload")])
    with pytest.raises(OSError):
        sync_once(store, client, KEY)
    assert json.loads(store.channels(KEY)[0]["cursor"]) == {"page": 2}
    assert store.status(KEY)["last_error"] == "OSError"
    # Reopen to prove cursor and message checkpoint survive process loss.
    resumed = Store(store.path)
    try:
        client = Client([([replace(message(), text="Edited human reply")], None)])
        status = sync_once(resumed, client, KEY)
        assert client.states == [{"page": 2}]
        assert status["messages"] == 1
        assert status["last_error"] is None
        assert next(iter(resumed.export(KEY)))["text"] == "Edited human reply"
        assert resumed.channels(KEY)[0]["cursor"] is None
    finally:
        resumed.close()


def test_slack_prunes_only_after_complete_scan(store):
    store.save_page(KEY, "C1", 0, [message(message_id="old")], None)
    sync_once(store, Client([([message()], {"page": 2})]), KEY, max_pages=1)
    assert store.status(KEY)["messages"] == 2
    sync_once(store, Client([([], None)]), KEY)
    assert [row["message_id"] for row in store.export(KEY)] == [message().message_id]


def test_discord_absence_does_not_delete_on_lost_history_permission(store):
    key = ("discord", "U1", "T1")
    store.connect(key, "test", ["C1"])
    store.save_page(key, "C1", 1, [message(source="discord")], None)
    store.save_page(key, "C1", 2, [], None)
    assert store.status(key)["messages"] == 1


def test_backoff_is_durable_and_does_not_consume_another_page(store):
    status = sync_once(store, Client([RateLimited(120)]), KEY)
    assert status["deferred"]
    assert status["retry_at"] > time.time() + 100
    client = Client([])
    assert sync_once(store, client, KEY)["deferred"]
    assert client.states == []
    assert json.loads(store.channels(KEY)[0]["cursor"]) == {"page": 1}


def test_invalid_cursor_restarts_without_discarding_cache(store):
    store.save_page(KEY, "C1", 0, [message()], {"page": 9})
    with pytest.raises(ProviderError):
        sync_once(store, Client([ProviderError("invalid_cursor")]), KEY)
    assert store.channels(KEY)[0]["cursor"] is None
    assert store.status(KEY)["messages"] == 1


def test_changed_identity_and_disconnected_credentials_stop_reads(store):
    client = Client([])
    client.identity = lambda: ("U1", "WRONG")
    with pytest.raises(ValueError, match="no longer matches"):
        sync_once(store, client, KEY)
    assert client.states == []

    def disconnected(_):
        raise ValueError("disconnected")

    with pytest.raises(ValueError, match="disconnected"):
        sync_once(store, client, KEY, credential_check=disconnected)
    assert client.states == []


def test_slack_paginates_threads_before_next_history_page():
    session = Session(
        [
            {
                "ok": True,
                "messages": [
                    {"ts": "1700000000.000001", "user": "U2", "text": "Hi", "reply_count": 2}
                ],
                "has_more": True,
                "response_metadata": {"next_cursor": "history-2"},
            },
            {
                "ok": True,
                "messages": [
                    {
                        "ts": "1700000000.000002",
                        "thread_ts": "1700000000.000001",
                        "user": "U1",
                        "text": "Yes",
                    }
                ],
                "has_more": True,
                "response_metadata": {"next_cursor": "reply-2"},
            },
            {
                "ok": True,
                "messages": [
                    {
                        "ts": "1700000000.000003",
                        "thread_ts": "1700000000.000001",
                        "bot_id": "B1",
                        "text": "Bot",
                    }
                ],
            },
            {"ok": True, "messages": []},
        ]
    )
    client = SlackClient("secret", session=session)
    state = client.initial_state()
    all_messages = []
    for _ in range(4):
        messages, state = client.page("U1", "T1", "C1", state)
        all_messages.extend(messages)
    assert state is None
    assert all_messages[1].is_self
    assert all_messages[1].message_id == "1700000000.000002"
    assert all_messages[1].reply_to_id == "1700000000.000001"
    assert all_messages[2].is_bot
    assert session.calls[2][1]["params"]["cursor"] == "reply-2"
    assert session.calls[3][1]["params"]["cursor"] == "history-2"
    assert all(item.source == "slack" for item in all_messages)
    assert all("secret" not in str(call) for call in session.calls)


def test_slack_deleted_thread_and_missing_pagination_cursor():
    client = SlackClient("secret", session=Session([{"ok": False, "error": "thread_not_found"}]))
    messages, state = client.page(
        "U1",
        "T1",
        "C1",
        {
            "latest": "1700000001.000001",
            "threads": ["1700000000.000001"],
            "history_done": True,
        },
    )
    assert messages == [] and state is None
    with pytest.raises(ProviderError):
        SlackClient.next_cursor({"has_more": True})


def test_discord_message_ids_threads_auth_and_pagination():
    session = Session(
        [
            {"id": "42", "bot": True},
            {"flags": 1 << 19},
            {"id": "88", "guild_id": "99", "type": 11},
            [
                {
                    "id": "200",
                    "channel_id": "88",
                    "author": {"id": "7"},
                    "content": "Reply",
                    "type": 19,
                    "message_reference": {"message_id": "100"},
                }
            ],
            [],
        ]
    )
    client = DiscordClient("secret", session=session)
    assert client.identity() == "42"
    client.validate_channel("88", "99")
    messages, state = client.page("7", "99", "88", {"before": "300"})
    assert state == {"before": "200"}
    assert messages[0].source == "discord"
    assert messages[0].is_self
    assert messages[0].thread_id == "88"
    assert messages[0].reply_to_id == "100"
    assert session.headers["Authorization"] == "Bot secret"
    assert client.page("7", "99", "88", state) == ([], None)


def test_discord_rejects_cross_server_channels_and_missing_intent():
    client = DiscordClient("secret", session=Session([{"guild_id": "OTHER", "type": 0}]))
    with pytest.raises(ValueError, match="selected server"):
        client.validate_channel("88", "99")
    client = DiscordClient("secret", session=Session([{"id": "42", "bot": True}, {"flags": 0}]))
    with pytest.raises(ValueError, match="Message Content"):
        client.identity()


@pytest.mark.parametrize("client_class", [SlackClient, DiscordClient])
def test_http_rate_limit_and_error_redaction(client_class):
    response = SimpleNamespace(
        status_code=429,
        headers={"Retry-After": "65"},
        json=lambda: {"retry_after": 70, "message": "private"},
    )
    client = client_class("secret", session=Session([response]))
    with pytest.raises(RateLimited) as error:
        client.get("test")
    assert error.value.seconds >= 65
    assert "private" not in str(error.value)
    assert client.session.calls[0][1]["allow_redirects"] is False


def test_gmail_bridge_labels_legacy_rows_and_excludes_drafts(tmp_path):
    store = GmailStore(tmp_path / "mail.db")
    try:
        with store.db:
            store.add_account("me@example.com", "test")
            store.replace_thread(
                "me@example.com",
                "thread",
                [
                    {
                        "id": "sent",
                        "thread_id": "thread",
                        "internal_date": 1,
                        "labels": ["SENT"],
                        "headers": {"from": "me@example.com"},
                        "body_text": "Sent reply",
                    },
                    {
                        "id": "draft",
                        "thread_id": "thread",
                        "internal_date": 2,
                        "labels": ["DRAFT"],
                        "headers": {},
                        "body_text": "Unsent",
                    },
                ],
            )
        rows = list(export_gmail(store.path, "me@example.com"))
        assert len(rows) == 1
        assert rows[0]["source"] == "gmail" and rows[0]["is_self"]
        assert rows[0]["usage"] == "context_only"
        assert store.thread_messages("me@example.com", "thread")[0]["source"] == "gmail"
    finally:
        store.close()


def test_jsonl_permissions_no_overwrite_and_source_validation(tmp_path):
    output = tmp_path / "messages.jsonl"
    write_jsonl([message().to_dict()], output)
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["source"] == "slack"
    with pytest.raises(FileExistsError):
        write_jsonl([], output)
    with pytest.raises(ValueError):
        message(source="untrusted")
    for source in ("gmail", "slack", "discord"):
        pair = Pair.from_dict({"recipient": "user", "thread": "Hi", "source": source})
        assert prompt_text(pair).startswith(f"Source: {source}\n")
    assert Pair.from_dict({"recipient": "user"}).source == "gmail"


def test_bundled_cli_dispatch_and_export(store, capsys):
    store.save_page(KEY, "C1", 1, [message()], None)
    args = ["--db", str(store.path), "--source", "slack", "--account", "U1", "--workspace", "T1"]
    assert worker_main(["messages", *args, "status"]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == "slack"
    output = store.path.parent / "export.jsonl"
    assert main([*args, "export", "--output", str(output)]) == 0
    assert json.loads(output.read_text())["usage"] == "context_only"
    assert main([*args, "export", "--output", str(output)]) == 1


def test_connect_verifies_slack_identity_before_saving_token(tmp_path, monkeypatch, capsys):
    from message_pipeline import __main__ as cli

    saved = {}
    vault = SimpleNamespace(set_password=lambda service, ref, token: saved.update({ref: token}))
    monkeypatch.setattr(cli, "keychain", lambda: vault)
    monkeypatch.setenv("TEST_SLACK_TOKEN", "synthetic-token")
    client = SlackClient(
        "synthetic-token",
        session=Session(
            [
                {"ok": True, "user_id": "U1", "team_id": "T1"},
                {"ok": True, "channel": {"id": "C1"}},
            ]
        ),
    )
    monkeypatch.setattr(cli, "client_for", lambda *_: client)
    args = ["--db", str(tmp_path / "messages.db"), "--source", "slack"]
    assert main([*args, "connect", "--channels", "C1", "--token-env", "TEST_SLACK_TOKEN"]) == 0
    result = capsys.readouterr()
    assert json.loads(result.out)["account_id"] == "U1"
    assert "synthetic-token" not in result.out + result.err
    assert list(saved.values()) == ["synthetic-token"]
    assert "synthetic-token" not in (tmp_path / "messages.db").read_bytes().decode(errors="ignore")

    client.session = Session([{"ok": True, "user_id": "U2", "team_id": "T1"}])
    saved.clear()
    assert (
        main(
            [
                *args,
                "--account",
                "U1",
                "connect",
                "--channels",
                "C1",
                "--token-env",
                "TEST_SLACK_TOKEN",
            ]
        )
        == 1
    )
    assert saved == {}


def test_context_only_exports_are_rejected_by_training_loader(tmp_path, monkeypatch):
    core = SimpleNamespace()
    optim = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=core, optimizers=optim))
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    monkeypatch.setitem(sys.modules, "mlx.optimizers", optim)
    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(load=None))
    monkeypatch.setitem(
        sys.modules, "mlx_lm.tuner.trainer", SimpleNamespace(TrainingArgs=None, train=None)
    )
    monkeypatch.setitem(
        sys.modules, "mlx_lm.tuner.utils", SimpleNamespace(linear_to_lora_layers=None)
    )
    monkeypatch.setitem(sys.modules, "mlx_lm.utils", SimpleNamespace(save_config=None))
    namespace = runpy.run_module("train.trainer", run_name="pipeline_test_trainer")
    path = tmp_path / "messages.jsonl"
    path.write_text(json.dumps({"usage": "context_only", "source": "discord"}) + "\n")
    with pytest.raises(ValueError, match="Context-only"):
        namespace["load_examples"](None, path, "base")


class Generator:
    version = "synthetic-model"

    def __init__(self):
        self.contexts = []

    def generate(self, context):
        self.contexts.append(context)
        return "A suggested reply"


def test_suggestions_source_context_idempotence_and_observed_human_reply(store):
    incoming = message(is_self=False, author_id="U2")
    store.save_page(KEY, "C1", 1, [incoming], None)
    generator = Generator()
    assert generate_suggestions(store, KEY, generator)["generated"] == 0  # Historical import.
    assert generate_suggestions(store, KEY, generator, include_history=True)["generated"] == 1
    assert generator.contexts[0]["source"] == "slack"
    assert generator.contexts[0]["conversation"] == [{"from": "them", "text": incoming.text}]
    assert generate_suggestions(store, KEY, generator, include_history=True)["generated"] == 0
    suggestion = next(store.suggestions(KEY))
    assert suggestion["state"] == "ready"
    assert suggestion["generated_text"] == "A suggested reply"
    sent = message(message_id="1799999999.000001", timestamp_ms=suggestion["created_at_ms"] + 1)
    # A reply in another thread must not close this suggestion.
    other = replace(sent, message_id="other", thread_id="another-thread")
    store.save_page(KEY, "C1", 2, [incoming, other], {"page": 2})
    with store.db:
        reconcile(store, KEY)
    assert next(store.suggestions(KEY))["state"] == "ready"
    store.save_page(KEY, "C1", 2, [sent], {"page": 3})
    with store.db:
        reconcile(store, KEY)
    suggestion = next(store.suggestions(KEY))
    assert suggestion["state"] == "replied"
    assert suggestion["observed_reply"]["source"] == "slack"
    assert suggestion["observed_reply"]["text"] == "A human reply"
    assert suggestion["suggestion_use_confirmed"] is False
    assert suggestion["usage"] == "context_only"


def test_fresh_incoming_is_eligible_and_dismissal_is_preserved(store):
    incoming = message(is_self=False, author_id="U2", timestamp_ms=time.time_ns() // 1_000_000)
    store.save_page(KEY, "C1", 1, [incoming], None)
    generator = Generator()
    assert generate_suggestions(store, KEY, generator)["generated"] == 1
    item = next(store.suggestions(KEY))
    with pytest.raises(ValueError):
        dismiss(store, ("slack", "OTHER", "T1"), item["id"])
    dismiss(store, KEY, item["id"])
    assert generate_suggestions(store, KEY, generator)["generated"] == 0
    assert next(store.suggestions(KEY))["state"] == "dismissed"


def test_changed_incoming_supersedes_older_suggestion_and_edits_refresh_context(store):
    incoming = message(is_self=False, author_id="U2")
    store.save_page(KEY, "C1", 1, [incoming], None)
    generator = Generator()
    generate_suggestions(store, KEY, generator, include_history=True)
    store.save_page(KEY, "C1", 1, [replace(incoming, text="Edited question")], None)
    generate_suggestions(store, KEY, generator, include_history=True)
    assert len(list(store.suggestions(KEY))) == 1
    assert generator.contexts[-1]["conversation"][-1]["text"] == "Edited question"
    newer = replace(
        incoming, message_id="1700000000.000003", timestamp_ms=incoming.timestamp_ms + 1
    )
    store.save_page(KEY, "C1", 1, [newer], {"page": 2})
    generate_suggestions(store, KEY, generator, include_history=True)
    assert [item["state"] for item in store.suggestions(KEY)] == ["superseded", "ready"]


def test_discord_suggestions_keep_source_and_skip_bot_and_self_messages(store):
    key = ("discord", "U1", "T1")
    store.connect(key, "test", ["C1"])
    incoming = message(source="discord", is_self=False, author_id="U2", thread_id="C1")
    bot = replace(incoming, message_id="bot", is_bot=True)
    store.save_page(key, "C1", 1, [incoming, bot], None)
    generator = Generator()
    generate_suggestions(store, key, generator, include_history=True)
    assert generator.contexts[0]["source"] == "discord"
    assert len(generator.contexts[0]["conversation"]) == 1
    sent = replace(
        incoming, message_id="sent", is_self=True, timestamp_ms=incoming.timestamp_ms + 1
    )
    store.save_page(key, "C1", 1, [sent], None)
    assert generate_suggestions(store, key, generator, include_history=True)["generated"] == 0


def test_busy_model_leaves_suggestion_queued(store):
    from gmail_agent.locking import AlreadyRunning

    store.save_page(KEY, "C1", 1, [message(is_self=False)], None)

    class Busy:
        def generate(self, context):
            raise AlreadyRunning("model is busy")

    assert generate_suggestions(store, KEY, Busy(), include_history=True)["generated"] == 0
    assert next(store.suggestions(KEY))["state"] == "queued"
    assert generate_suggestions(store, KEY, Generator(), include_history=True)["generated"] == 1
