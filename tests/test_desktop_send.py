import base64
import copy
from email import policy
from email.parser import BytesParser

import pytest

from gmail_agent.client import GmailClient, GmailError, PublishUncertain
from gmail_agent.desktop import inbox, send, snapshot
from gmail_agent.store import Store


class Client:
    def __init__(self):
        self.current = {
            "id": "remote-message",
            "thread_id": "thread",
            "internal_date": 2000,
            "body_text": "Thanks, I can join.",
            "has_attachments": False,
            "headers": {
                "from": "me@example.com",
                "to": "Friend <friend@example.com>",
                "subject": "Meeting",
                "in-reply-to": "<parent@example.com>",
                "references": "<parent@example.com>",
            },
        }
        self.calls = []
        self.error = None

    def get_draft(self, _):
        return copy.deepcopy(self.current)

    def send_draft(self, draft, thread, raw):
        self.calls.append((draft, thread, raw))
        if self.error:
            raise self.error
        return {"id": "sent-message", "threadId": thread}


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "mail.db")
    with store.db:
        store.add_account("me@example.com", "me@example.com")
        store.ensure_thread("me@example.com", "thread")
        store.enqueue(
            "me@example.com", "thread", "trigger", {"conversation": [{"text": "Can you join?"}]}
        )
        row = store.active_drafts("me@example.com")[0]
        store.set_state(
            row["id"],
            "published",
            gmail_draft_id="remote-draft",
            generated_text="Original reply",
            published_at=1000,
        )
    client = Client()
    request = {
        "generationId": row["id"],
        "gmailDraftId": "remote-draft",
        "snapshot": snapshot(client.current),
        "body": "Thanks, 2:00 PM works.",
    }
    yield store, client, request
    store.close()


def test_inbox_uses_current_gmail_text_and_preserves_delivery_identity(setup):
    store, client, request = setup
    result = inbox(store, client, "me@example.com")
    draft = result["drafts"][0]
    assert draft["body"] == client.current["body_text"]
    assert draft["address"] == "Friend <friend@example.com>"
    assert draft["delivery"]["snapshot"] == request["snapshot"]
    assert draft["delivery"]["generationId"] == request["generationId"]


@pytest.mark.parametrize(
    "body", ["", "   ", "At [TIME]", "On [DATE]", "[PERSON NAME]", "x" * 60001]
)
def test_unresolved_or_invalid_replies_never_send(setup, body):
    store, client, request = setup
    with pytest.raises(ValueError):
        send(store, client, "me@example.com", {**request, "body": body})
    assert not client.calls


def test_changed_remote_draft_requires_a_fresh_review(setup):
    store, client, request = setup
    client.current["headers"]["to"] = "someone-else@example.com"
    with pytest.raises(ValueError, match="changed"):
        send(store, client, "me@example.com", request)
    assert not client.calls


@pytest.mark.parametrize("change", ["attachments", "cc", "bcc", "from", "recipients"])
def test_unsupported_remote_content_cannot_be_silently_replaced(setup, change):
    store, client, request = setup
    if change == "attachments":
        client.current["has_attachments"] = True
    else:
        client.current["headers"][{"recipients": "to"}.get(change, change)] = (
            "other@example.com, third@example.com"
        )
    request["snapshot"] = snapshot(client.current)
    with pytest.raises(ValueError):
        send(store, client, "me@example.com", request)
    assert not client.calls


def test_other_accounts_cannot_send_the_draft(setup):
    store, client, request = setup
    with pytest.raises(ValueError, match="belong"):
        send(store, client, "other@example.com", request)
    assert not client.calls


def test_send_uses_reviewed_local_body_and_marks_sent_only_after_ack(setup):
    store, client, request = setup
    assert send(store, client, "me@example.com", request)["status"] == "sent"
    raw = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(client.calls[0][2])
    )
    assert raw.get_content().strip() == request["body"]
    assert raw["To"] == "Friend <friend@example.com>"
    assert raw["In-Reply-To"] == "<parent@example.com>"
    assert store.draft(request["generationId"])["state"] == "sent"
    assert next(iter(store.feedback("me@example.com")))["final_text"] == request["body"]
    assert send(store, client, "me@example.com", request)["status"] == "sent"
    assert len(client.calls) == 1


def test_uncertain_outcome_is_durable_and_never_replayed(setup):
    store, client, request = setup
    client.error = PublishUncertain()
    assert send(store, client, "me@example.com", request)["status"] == "uncertain"
    assert store.draft(request["generationId"])["state"] == "published"
    reopened = Store(store.path)
    try:
        assert send(reopened, client, "me@example.com", request)["status"] == "uncertain"
        assert len(client.calls) == 1
    finally:
        reopened.close()


def test_definitive_rejection_keeps_draft_and_allows_explicit_retry(setup):
    store, client, request = setup
    client.error = GmailError(403, "send")
    with pytest.raises(GmailError):
        send(store, client, "me@example.com", request)
    assert store.draft(request["generationId"])["state"] == "published"
    client.error = None
    assert send(store, client, "me@example.com", request)["status"] == "sent"
    assert len(client.calls) == 2


@pytest.mark.parametrize("outcome", ["timeout", "server", "invalid"])
def test_rest_send_never_retries_an_uncertain_write(outcome):
    from types import SimpleNamespace

    import requests

    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        if outcome == "timeout":
            raise requests.Timeout()
        return SimpleNamespace(
            status_code=503 if outcome == "server" else 200, ok=True, json=dict
        )

    with pytest.raises(PublishUncertain):
        GmailClient(SimpleNamespace(post=post)).send_draft("draft", "thread", "raw")
    assert len(calls) == 1
    assert calls[0][1]["json"]["id"] == "draft"
