import base64
import copy
import sqlite3
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import pytest

from gmail_agent.__main__ import recover
from gmail_agent.client import GmailError, PublishUncertain
from gmail_agent.locking import AlreadyRunning, process_lock, worker_lock
from gmail_agent.store import Store
from gmail_agent.sync import backfill, sync_mailbox
from gmail_agent.worker import run_cycle, run_forever


def message(mid="incoming", thread="thread-1", timestamp=200, labels=None, body="Can we meet?"):
    return {
        "id": mid,
        "thread_id": thread,
        "internal_date": timestamp,
        "labels": labels if labels is not None else ["INBOX"],
        "headers": {
            "from": "Friend <friend@example.com>",
            "to": "me@example.com",
            "subject": "Meeting",
            "message-id": f"<{mid}@example.com>",
        },
        "body_text": body,
    }


class FakeGmail:
    """Models Gmail's changing draft message IDs and independent SENT messages."""

    def __init__(self, messages=()):
        self.threads = {}
        self.drafts = {}
        self.cursor = 10
        self.events = []
        self.creates = []
        self.lose_response = False
        self.read_error = None
        self.expired_cursor = None
        self.pages = None
        self.page_reads = []
        self.draft_list_reads = 0
        self.thread_reads = []
        for item in messages:
            self.add(item)

    def add(self, item, event_type="messagesAdded"):
        self.threads.setdefault(item["thread_id"], []).append(copy.deepcopy(item))
        self.cursor += 1
        self.events.append(
            {
                "id": str(self.cursor),
                event_type: [{"message": {"id": item["id"], "threadId": item["thread_id"]}}],
            }
        )

    def profile(self):
        return {"emailAddress": "me@example.com", "historyId": str(self.cursor)}

    def threads_page(self, page_token=None, query=""):
        self.page_reads.append(page_token)
        if self.pages is not None:
            return copy.deepcopy(self.pages[page_token])
        return {"threads": [{"id": tid} for tid in self.threads]}

    def history_page(self, history_id, page_token=None):
        if history_id == self.expired_cursor:
            raise GmailError(404, "history")
        return {
            "historyId": str(self.cursor),
            "history": [h for h in self.events if int(h["id"]) > int(history_id)],
        }

    def get_thread(self, thread_id):
        self.thread_reads.append(thread_id)
        if self.read_error:
            raise self.read_error
        return sorted(
            copy.deepcopy(self.threads.get(thread_id, [])),
            key=lambda item: (item["internal_date"], item["id"]),
        )

    def list_drafts(self):
        self.draft_list_reads += 1
        return [
            {"id": did, "message": {"id": item["id"], "threadId": item["thread_id"]}}
            for did, item in self.drafts.items()
        ]

    def get_draft(self, draft_id):
        return copy.deepcopy(self.drafts[draft_id])

    def create_draft(self, thread_id, raw):
        mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
        self.creates.append((thread_id, mime))
        did = f"draft-{len(self.creates)}"
        item = message(f"draft-message-{did}", thread_id, 1000, ["DRAFT"], mime.get_content())
        item["headers"].update({name.lower(): str(value) for name, value in mime.items()})
        self.drafts[did] = item
        self.add(item)
        if self.lose_response:
            raise PublishUncertain("simulated connection loss after Gmail accepted the draft")
        return {"id": did, "message": {"id": item["id"], "threadId": thread_id}}

    def discard(self, did):
        item = self.drafts.pop(did)
        self.threads[item["thread_id"]] = [
            m for m in self.threads[item["thread_id"]] if m["id"] != item["id"]
        ]
        self.cursor += 1
        self.events.append(
            {
                "id": str(self.cursor),
                "messagesDeleted": [{"message": {"id": item["id"], "threadId": item["thread_id"]}}],
            }
        )

    def send(self, did, body="Tomorrow works for me.", timestamp=1200):
        thread_id = self.drafts[did]["thread_id"]
        self.discard(did)
        item = message(f"sent-{timestamp}", thread_id, timestamp, ["SENT"], body)
        item["headers"]["from"] = "me@example.com"
        self.add(item)


class Generator:
    version = "test-model:test-adapter"
    adapter_name = "test-adapter"

    def __init__(self):
        self.calls = []
        self.fail = False
        self.on_generate = None
        self.text = "[DATE] works for me."
        self.closes = 0

    def generate(self, context):
        self.calls.append(context)
        if self.on_generate:
            self.on_generate()
        if self.fail:
            raise RuntimeError("simulated model failure")
        return self.text

    def close(self):
        self.closes += 1


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path / "mail.db")
    with store.db:
        store.add_account("me@example.com", "test-keychain-ref", activated_at=150)
    monkeypatch.setattr("gmail_agent.replies.now_ms", lambda: 1000)
    yield store, "me@example.com", Generator()
    store.close()


def test_full_lifecycle_native_draft_edit_send_and_next_turn(setup):
    store, account, generator = setup
    client = FakeGmail([message(timestamp=100, mid="old")])
    run_cycle(store, client, account, generator)
    assert not client.creates  # Historical import must not reply to the archive.
    client.add(message())
    run_cycle(store, client, account, generator)
    assert len(client.creates) == 1
    thread_id, mime = client.creates[0]
    assert thread_id == "thread-1"
    assert mime["In-Reply-To"] == "<incoming@example.com>"
    assert mime["To"] == "Friend <friend@example.com>"
    assert "Can we meet?" in generator.calls[0]["conversation"][-1]["text"]
    run_cycle(store, client, account, generator)
    assert len(client.creates) == 1  # Own DRAFT events cannot retrigger generation.
    client.send("draft-1")
    run_cycle(store, client, account, generator)
    feedback = list(store.feedback(account))
    assert len(feedback) == 1
    assert feedback[0]["generated_text"] == "[DATE] works for me."
    assert feedback[0]["final_text"] == "Tomorrow works for me."
    assert feedback[0]["changed"] is True
    client.add(message("next-question", timestamp=1400))
    run_cycle(store, client, account, generator)
    assert len(client.creates) == 2  # A later turn in the same thread gets its own draft.


def test_separate_backfill_still_queues_post_activation_mail(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    assert backfill(store, client, account)
    run_cycle(store, client, account, generator)
    assert len(client.creates) == 1


def test_backfill_checkpoints_and_restarts_without_duplicates(setup):
    store, account, _ = setup
    client = FakeGmail([message("a", "one"), message("b", "two")])
    client.pages = {
        None: {"threads": [{"id": "one"}], "nextPageToken": "page2"},
        "page2": {"threads": [{"id": "two"}]},
    }
    assert not backfill(store, client, account, max_pages=1)
    assert store.account(account)["backfill_page"] == "page2"
    assert backfill(store, client, account)
    assert client.page_reads == [None, "page2"]
    assert backfill(store, client, account, reset=True)
    assert store.status(account)["messages"] == 2


def test_import_race_is_caught_from_anchor(setup):
    store, account, generator = setup
    client = FakeGmail([message("old", timestamp=100)])
    original = client.threads_page

    def page(*args):
        result = original(*args)
        client.add(message("arrived-during-import", "another"))
        return result

    client.threads_page = page
    run_cycle(store, client, account, generator)
    assert client.creates[0][0] == "another"


def test_failed_fetch_does_not_advance_cursor_or_queue_partial_work(setup):
    store, account, generator = setup
    client = FakeGmail()
    run_cycle(store, client, account, generator)
    cursor = store.account(account)["history_id"]
    client.add(message())
    client.read_error = RuntimeError("offline")
    with pytest.raises(RuntimeError):
        run_cycle(store, client, account, generator)
    assert store.account(account)["history_id"] == cursor
    assert not store.active_drafts(account)
    client.read_error = None
    run_cycle(store, client, account, generator)
    assert len(client.creates) == 1


def test_queue_failure_rolls_back_mail_and_cursor(setup, monkeypatch):
    store, account, generator = setup
    client = FakeGmail()
    run_cycle(store, client, account, generator)
    cursor = store.account(account)["history_id"]
    client.add(message())

    def broken(*args):
        raise RuntimeError("database failure")

    monkeypatch.setattr("gmail_agent.replies.queue_candidates", broken)
    with pytest.raises(RuntimeError):
        sync_mailbox(store, client, account)
    assert store.account(account)["history_id"] == cursor
    assert not store.thread_messages(account, "thread-1")


def test_model_failure_retains_work_after_cursor_advances_and_restart(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    generator.fail = True
    run_cycle(store, client, account, generator)
    assert store.account(account)["history_id"] == str(client.cursor)
    assert store.active_drafts(account)[0]["state"] == "queued"
    path = store.path
    generator.fail = False
    restarted = Store(path)
    try:
        run_cycle(restarted, client, account, generator)
        assert len(client.creates) == 1
    finally:
        restarted.close()


def test_existing_human_draft_is_not_modified(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    human = message("human-draft-message", timestamp=300, labels=["DRAFT"], body="My reply.")
    client.add(human)
    client.drafts["human-draft"] = human
    run_cycle(store, client, account, generator)
    assert client.drafts["human-draft"]["body_text"] == "My reply."
    assert not generator.calls


def test_discarded_draft_is_not_recreated(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    run_cycle(store, client, account, generator)
    client.discard("draft-1")
    run_cycle(store, client, account, generator)
    run_cycle(store, client, account, generator)
    assert len(client.creates) == 1
    assert store.status(account)["drafts"] == {"dismissed": 1}
    assert not list(store.feedback(account))


def test_unknown_publish_recovers_existing_draft_without_another_post(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    client.lose_response = True
    run_cycle(store, client, account, generator)
    assert store.active_drafts(account)[0]["state"] == "publishing"
    client.lose_response = False
    run_cycle(store, client, account, generator)
    assert store.active_drafts(account)[0]["state"] == "published"
    assert len(client.creates) == 1


def test_unknown_publish_with_no_draft_blocks_retry(setup):
    store, account, generator = setup
    client = FakeGmail([message()])

    def unknown(*args):
        raise PublishUncertain("unknown")

    client.create_draft = unknown
    run_cycle(store, client, account, generator)
    run_cycle(store, client, account, generator)
    assert len(generator.calls) == 1
    assert store.active_drafts(account)[0]["state"] == "publishing"


def test_sent_during_generation_supersedes_stale_reply(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    generator.on_generate = lambda: client.add(
        message("manual-sent", timestamp=900, labels=["SENT"])
    )
    run_cycle(store, client, account, generator)
    assert not client.creates
    assert store.status(account)["drafts"] == {"superseded": 1}


def test_human_draft_appearing_during_generation_is_preserved(setup):
    store, account, generator = setup
    client = FakeGmail([message()])

    def human_draft():
        item = message("human", timestamp=900, labels=["DRAFT"])
        client.drafts["human"] = item
        client.add(item)

    generator.on_generate = human_draft
    run_cycle(store, client, account, generator)
    assert not client.creates
    assert len(generator.calls) == 1
    assert client.draft_list_reads == 1
    assert store.draft_ids(account, "thread-1") == []  # Still the earlier index.
    assert client.drafts["human"]["body_text"] == "Can we meet?"
    assert store.draft(1)["state"] == "superseded"


def test_expired_history_resync_preserves_existing_reply_record(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    run_cycle(store, client, account, generator)
    client.expired_cursor = store.account(account)["history_id"]
    run_cycle(store, client, account, generator)
    assert len(client.creates) == 1
    assert store.status(account)["drafts"] == {"published": 1}


@pytest.mark.parametrize("change", ["messagesAdded", "labelsAdded"])
def test_sent_event_types_capture_final_message(setup, change):
    store, account, generator = setup
    client = FakeGmail([message()])
    run_cycle(store, client, account, generator)
    client.discard("draft-1")
    client.add(message("final", timestamp=1500, labels=["SENT"]), event_type=change)
    run_cycle(store, client, account, generator)
    assert next(store.feedback(account))["sent_message_id"] == "final"


def test_thread_mapping_uses_first_sent_and_is_account_scoped(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    run_cycle(store, client, account, generator)
    client.send("draft-1", timestamp=1200)
    client.add(message("later-sent", timestamp=1400, labels=["SENT"]))
    with store.db:
        other = store.add_account("other@example.com", "other", 150)
        store.replace_thread(
            other, "thread-1", [message("wrong-account", timestamp=1100, labels=["SENT"])]
        )
    run_cycle(store, client, account, generator)
    assert next(store.feedback(account))["sent_message_id"] == "sent-1200"
    assert not list(store.feedback(other))


@pytest.mark.parametrize(
    "headers,labels",
    [
        ({"list-unsubscribe": "<https://example.com/unsub>"}, ["INBOX"]),
        ({"auto-submitted": "auto-generated"}, ["INBOX"]),
        ({"precedence": "bulk"}, ["INBOX"]),
        ({"from": "no-reply@example.com"}, ["INBOX"]),
        ({}, ["INBOX", "TRASH"]),
        ({}, ["INBOX", "SPAM"]),
        ({}, ["SENT"]),
    ],
)
def test_ineligible_messages_never_generate(setup, headers, labels):
    store, account, generator = setup
    item = message(labels=labels)
    item["headers"].update(headers)
    run_cycle(store, FakeGmail([item]), account, generator)
    assert not generator.calls


def test_database_enforces_one_active_draft_per_thread(setup):
    store, account, _ = setup
    with store.db:
        store.ensure_thread(account, "thread-1")
        assert store.enqueue(account, "thread-1", "one", {})
        assert not store.enqueue(account, "thread-1", "two", {})
        store.set_state(1, "sent")
        assert store.enqueue(account, "thread-1", "two", {})
        with pytest.raises(sqlite3.IntegrityError):
            store.set_state(1, "queued")


def test_single_instance_lock_and_release(tmp_path):
    path = tmp_path / "worker.lock"
    with process_lock(path), pytest.raises(AlreadyRunning), process_lock(path):
        pass
    with process_lock(path):
        pass


def test_schedule_defaults_to_30_minutes_and_retries_after_error(setup, monkeypatch):
    store, account, generator = setup
    delays = []

    class Stop:
        def is_set(self):
            return bool(delays)

        def wait(self, delay):
            delays.append(delay)

    def fail(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("gmail_agent.worker.run_cycle", fail)
    run_forever(store, FakeGmail(), account, generator, stop=Stop())
    assert 1799 < delays[0] <= 1800


def test_busy_worker_retries_after_jitter_then_returns_to_normal_schedule(setup, monkeypatch):
    store, account, generator = setup
    client = FakeGmail([message()])
    delays = []
    calls = []
    original = generator.generate

    def generate(context):
        calls.append(context)
        if len(calls) == 1:
            raise AlreadyRunning("another account is generating")
        return original(context)

    class Stop:
        def is_set(self):
            return len(delays) == 2

        def wait(self, delay):
            delays.append(delay)

    def jitter(low, high):
        assert (low, high) == (60, 300)
        return 120

    monkeypatch.setattr(generator, "generate", generate)
    monkeypatch.setattr("gmail_agent.worker.random.uniform", jitter)
    run_forever(store, client, account, generator, stop=Stop())
    assert delays[0] == 120
    assert 1799 < delays[1] <= 1800
    assert store.draft(1)["attempts"] == 1
    assert store.draft(1)["state"] == "published"
    assert len(client.creates) == 1
    assert generator.closes == 2


def test_cycle_health_persists_failure_without_private_error_text(setup, monkeypatch):
    store, account, generator = setup
    client = FakeGmail()
    run_cycle(store, client, account, generator)
    success_at = store.status(account)["worker"]["last_success_at"]
    assert success_at is not None

    def offline(*_):
        raise RuntimeError("private email body and credential")

    with monkeypatch.context() as patch:
        patch.setattr(client, "history_page", offline)
        with pytest.raises(RuntimeError):
            run_cycle(store, client, account, generator)
    path = store.path
    persisted = Store(path)
    try:
        health = persisted.db.execute("SELECT * FROM worker_health").fetchone()
        assert health["last_error"] == "RuntimeError"
        assert health["state"] == "error"
        assert health["last_success_at"] == success_at
    finally:
        persisted.close()
    run_cycle(store, client, account, generator)
    assert store.status(account)["worker"]["state"] == "idle"
    assert store.status(account)["worker"]["last_error"] is None


def test_paginated_history_commits_only_after_last_page(setup, monkeypatch):
    store, account, _ = setup
    client = FakeGmail()
    sync_mailbox(store, client, account)
    original_cursor = store.account(account)["history_id"]
    client.add(message("first", "one"))
    client.add(message("second", "two"))
    calls = []
    fail_second_page = True

    def history(cursor, token=None):
        calls.append((cursor, token))
        if token is None:
            return {"historyId": "11", "history": [client.events[0]], "nextPageToken": "next"}
        if fail_second_page:
            raise GmailError(503, "read")
        return {"historyId": "12", "history": [client.events[1]]}

    monkeypatch.setattr(client, "history_page", history)
    with pytest.raises(GmailError):
        sync_mailbox(store, client, account)
    assert store.account(account)["history_id"] == original_cursor
    assert store.status(account)["messages"] == 0
    assert not store.active_drafts(account)
    fail_second_page = False
    result = sync_mailbox(store, client, account)
    assert result["queued"] == 2
    assert store.account(account)["history_id"] == "12"
    assert calls == [(original_cursor, None), (original_cursor, "next")] * 2


def test_new_incoming_during_generation_requeues_latest_turn(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    generator.on_generate = lambda: client.add(message("newer", timestamp=300))
    run_cycle(store, client, account, generator)
    assert not client.creates
    assert store.draft(1)["state"] == "superseded"
    assert store.draft(2)["trigger_message_id"] == "newer"
    generator.on_generate = None
    run_cycle(store, client, account, generator)
    assert len(client.creates) == 1
    assert client.creates[0][1]["In-Reply-To"] == "<newer@example.com>"


def test_disappearing_uncertain_draft_does_not_block_other_threads(setup, monkeypatch):
    store, account, generator = setup
    client = FakeGmail([message()])
    client.lose_response = True
    run_cycle(store, client, account, generator)
    assert store.draft(1)["state"] == "publishing"
    client.lose_response = False
    client.add(message("another", "thread-2"))

    def disappeared(draft_id):
        client.discard(draft_id)
        raise GmailError(404, "read")

    monkeypatch.setattr(client, "get_draft", disappeared)
    run_cycle(store, client, account, generator)
    assert store.draft(1)["state"] == "publishing"
    assert store.draft(2)["state"] == "published"
    assert len(client.creates) == 2


def test_manual_recovery_refuses_retry_with_existing_draft_then_adopts(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    client.lose_response = True
    run_cycle(store, client, account, generator)
    args = SimpleNamespace(draft_id=1, adopt=None, dismiss=False, retry=True)
    with pytest.raises(ValueError, match="already exists"):
        recover(store, client, account, args)
    assert store.draft(1)["state"] == "publishing"
    args.adopt = "draft-1"
    result = recover(store, client, account, args)
    assert result == {"id": 1, "state": "published"}
    run_cycle(store, client, account, generator)
    assert len(client.creates) == 1


def test_manual_dismiss_keeps_remote_draft(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    run_cycle(store, client, account, generator)
    args = SimpleNamespace(draft_id=1, adopt=None, dismiss=True, retry=False)
    assert recover(store, client, account, args)["state"] == "dismissed"
    run_cycle(store, client, account, generator)
    assert "draft-1" in client.drafts
    assert len(client.creates) == 1


def test_uncertain_publication_recovers_multiline_smtp_body(setup):
    store, account, generator = setup
    generator.text = "Hi,\n\nSounds good.\nThanks"
    client = FakeGmail([message()])
    client.lose_response = True
    run_cycle(store, client, account, generator)
    assert client.drafts["draft-1"]["body_text"] == "Hi,\r\n\r\nSounds good.\r\nThanks\r\n"
    client.lose_response = False
    run_cycle(store, client, account, generator)
    assert store.draft(1)["state"] == "published"
    assert len(client.creates) == 1


@pytest.mark.parametrize("edited", [False, True])
def test_multiline_feedback_ignores_only_transport_line_endings(setup, edited):
    store, account, generator = setup
    generator.text = "Hi,\n\nSounds good.\nThanks"
    client = FakeGmail([message()])
    run_cycle(store, client, account, generator)
    body = client.drafts["draft-1"]["body_text"]
    if edited:
        body = body.replace("Sounds good.", "Sounds  good.")
    client.send("draft-1", body=body)
    run_cycle(store, client, account, generator)
    feedback = next(store.feedback(account))
    assert feedback["changed"] is edited
    assert (feedback["draft"] != feedback["final"]) is edited
    assert "\r" not in feedback["final"]
    assert feedback["final_text"] == body


def test_separate_sent_reply_does_not_become_feedback_for_surviving_draft(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    run_cycle(store, client, account, generator)
    original = copy.deepcopy(client.drafts["draft-1"])
    client.add(message("phone-reply", timestamp=1300, labels=["SENT"]))
    run_cycle(store, client, account, generator)
    assert store.draft(1)["state"] == "superseded"
    assert not list(store.feedback(account))
    assert client.drafts["draft-1"] == original
    # Removing the old draft later must not retroactively pair the phone reply.
    client.discard("draft-1")
    run_cycle(store, client, account, generator)
    assert not list(store.feedback(account))
    client.add(message("new-turn", timestamp=1400))
    run_cycle(store, client, account, generator)
    assert len(client.creates) == 2


def test_sent_generation_keeps_feedback_when_new_human_draft_exists(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    run_cycle(store, client, account, generator)
    client.send("draft-1", body=generator.text)
    human = message("new-human-draft", timestamp=1400, labels=["DRAFT"], body="Another thought")
    client.drafts["human"] = human
    client.add(human)
    run_cycle(store, client, account, generator)
    assert store.draft(1)["state"] == "sent"
    feedback = next(store.feedback(account))
    assert feedback["final_text"] == generator.text
    assert not feedback["changed"]
    assert client.drafts["human"] == human
    assert len(client.creates) == 1


def test_unconfirmed_publication_is_not_assumed_sent(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    client.lose_response = True
    run_cycle(store, client, account, generator)
    client.send("draft-1")
    run_cycle(store, client, account, generator)
    assert store.draft(1)["state"] == "superseded"
    assert not list(store.feedback(account))


def test_send_between_draft_listing_and_thread_fetch_waits_for_next_index(setup, monkeypatch):
    store, account, generator = setup
    client = FakeGmail([message()])
    run_cycle(store, client, account, generator)
    original = client.list_drafts

    def list_then_send():
        drafts = original()
        client.send("draft-1")
        return drafts

    with monkeypatch.context() as patch:
        patch.setattr(client, "list_drafts", list_then_send)
        run_cycle(store, client, account, generator)
    assert store.draft(1)["state"] == "published"
    assert not list(store.feedback(account))
    run_cycle(store, client, account, generator)
    assert store.draft(1)["state"] == "sent"
    assert len(list(store.feedback(account))) == 1
    assert len(client.creates) == 1


def test_one_draft_listing_per_cycle_with_multiple_queued_threads(setup):
    store, account, generator = setup
    client = FakeGmail([message(str(n), str(n)) for n in range(20)])
    assert run_cycle(store, client, account, generator)["published"] == 20
    assert client.draft_list_reads == 1


def test_unchanged_human_drafts_are_not_refetched_but_changed_sets_are(setup):
    store, account, _ = setup
    client = FakeGmail()
    human = message("human", timestamp=300, labels=["DRAFT"])
    client.add(human)
    client.drafts["human"] = human
    sync_mailbox(store, client, account)
    client.thread_reads.clear()
    sync_mailbox(store, client, account)
    assert not client.thread_reads
    # Draft index changes must be caught even before the history event arrives.
    del client.drafts["human"]
    client.threads["thread-1"] = []
    sync_mailbox(store, client, account)
    assert client.thread_reads == ["thread-1"]
    client.thread_reads.clear()
    client.drafts["replacement"] = human
    client.threads["thread-1"] = [human]
    sync_mailbox(store, client, account)
    assert client.thread_reads == ["thread-1"]


def test_cycle_releases_generator_on_success_and_sync_failure(setup):
    store, account, generator = setup
    client = FakeGmail([message()])
    run_cycle(store, client, account, generator)
    assert generator.closes == 1
    client.read_error = GmailError(503, "read")
    with pytest.raises(GmailError):
        run_cycle(store, client, account, generator)
    assert generator.closes == 2


def test_busy_model_defers_generation_without_exhausting_retries(setup, monkeypatch):
    store, account, generator = setup
    client = FakeGmail([message()])

    def busy(_):
        raise AlreadyRunning("held by training")

    with monkeypatch.context() as patch:
        patch.setattr(generator, "generate", busy)
        for _ in range(4):
            run_cycle(store, client, account, generator)
        assert store.draft(1)["state"] == "queued"
        assert store.draft(1)["attempts"] == 0
    run_cycle(store, client, account, generator)
    assert len(client.creates) == 1


def test_worker_locks_isolate_accounts_and_respect_legacy_workers(tmp_path):
    db = tmp_path / "mail.db"
    with worker_lock(db, "me@example.com"):
        with worker_lock(db, "other@example.com"):
            pass
        with pytest.raises(AlreadyRunning), worker_lock(db, "ME@example.com"):
            pass
        with pytest.raises(AlreadyRunning), process_lock(db.with_suffix(".worker.lock")):
            pass
    with (
        process_lock(db.with_suffix(".worker.lock")),
        pytest.raises(AlreadyRunning),
        worker_lock(db, "me@example.com"),
    ):
        pass


def test_feedback_exports_training_edit_with_exact_saved_context(setup):
    from train import format as training_format

    store, account, generator = setup
    client = FakeGmail(
        [
            message("old", timestamp=50),
            message("earlier", timestamp=100),
            message("mine", timestamp=120, labels=["SENT"], body="My earlier reply"),
            message("latest", timestamp=200),
        ]
    )
    run_cycle(store, client, account, generator)
    saved = generator.calls[0]["conversation"]
    # The whole cleaned thread, as training exports it; the drafter trims to fit.
    assert [m["from"] for m in saved] == ["them", "them", "me", "them"]
    assert saved[2]["text"] == "My earlier reply"
    client.send("draft-1")
    client.add(message("later-incoming", timestamp=1400))
    run_cycle(store, client, account, generator)
    feedback = next(store.feedback(account))
    assert feedback["adapter_name"] == generator.adapter_name
    assert feedback["conversation"] == saved
    assert feedback["draft"] == "[DATE] works for me."
    assert feedback["final"] == "Tomorrow works for me."
    assert feedback["type"] == "train"
    assert feedback["source"] == feedback["context"]["source"] == "gmail"
    if hasattr(training_format, "Edit"):
        edit = training_format.Edit.from_dict(feedback)
        assert edit.adapter_name == generator.adapter_name
        assert [m.sender for m in edit.context.messages] == ["them", "them", "me", "them"]
        assert edit.draft == feedback["draft"]
        assert edit.final == feedback["final"]


def test_legacy_feedback_reconstructs_only_saved_message_ids(setup):
    import json

    store, account, generator = setup
    client = FakeGmail([message()])
    run_cycle(store, client, account, generator)
    # Simulate a published record created before structured contexts were added.
    context = json.loads(store.draft(1)["context"])
    del context["conversation"]
    del context["adapter_name"]
    del context["source"]
    with store.db:
        store.set_state(1, "published", context=json.dumps(context))
    client.send("draft-1")
    client.add(message("new-incoming", timestamp=1400, body="A different question"))
    run_cycle(store, client, account, generator)
    feedback = next(store.feedback(account))
    assert feedback["adapter_name"] == "default"
    assert feedback["source"] == feedback["context"]["source"] == "gmail"
    assert feedback["conversation"] == [{"from": "them", "text": "Can we meet?"}]
