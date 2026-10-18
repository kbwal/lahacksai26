import base64
from email import policy
from email.parser import BytesParser

import pytest
import requests

from gmail_agent.client import (
    GmailClient,
    GmailError,
    PublishUncertain,
    body_text,
    reply_raw,
    reply_recipient,
)


def encoded(value):
    if isinstance(value, str):
        value = value.encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def part(mime, value, **kwargs):
    return {"mimeType": mime, "body": {"data": encoded(value)}, **kwargs}


class Response:
    def __init__(self, status, data=None):
        self.status_code = status
        self.ok = 200 <= status < 400
        self.data = data

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, *args, **kwargs):
        return self.request("GET", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self.request("POST", *args, **kwargs)


def test_mime_prefers_plain_and_excludes_attachments():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    part("text/html", "<p>HTML duplicate</p>"),
                    part("text/plain", "Actual reply"),
                ],
            },
            part("text/plain", "Secret attachment", filename="attached.txt"),
            {"mimeType": "message/rfc822", "parts": [part("text/plain", "forwarded file")]},
        ],
    }
    assert body_text(payload) == "Actual reply"


def test_html_charset_entities_and_hidden_tags():
    assert (
        body_text(
            part("text/html", "<style>hidden</style><p>Hello &amp; goodbye</p><script>x</script>")
        )
        == "Hello & goodbye"
    )
    assert (
        body_text(
            part(
                "text/plain",
                b"caf\xe9",
                headers=[{"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}],
            )
        )
        == "café"
    )


def test_reply_preserves_thread_headers_and_unicode():
    trigger = {
        "headers": {
            "from": "Name <sender@example.com>",
            "reply-to": "Replies <reply@example.com>",
            "subject": "Réunion",
            "message-id": "<parent@example.com>",
            "references": "<older@example.com>",
        }
    }
    raw = reply_raw("me@example.com", trigger, "Bonjour, à bientôt.")
    mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
    assert mime["To"] == "Replies <reply@example.com>"
    assert mime["Subject"] == "Réunion"
    assert mime["References"] == "<older@example.com> <parent@example.com>"
    assert mime["In-Reply-To"] == "<parent@example.com>"
    assert mime.get_content().strip() == "Bonjour, à bientôt."


@pytest.mark.parametrize(
    "value", ["invalid", "a@example.com, b@example.com", "a@example.com\r\nBcc: bad@example.com"]
)
def test_invalid_reply_recipient_is_rejected(value):
    assert reply_recipient({"headers": {"from": value}}) is None


def test_reads_retry_with_backoff_and_timeout():
    delays = []
    session = Session(
        [requests.Timeout(), Response(429), Response(503), Response(200, {"historyId": "cursor"})]
    )
    client = GmailClient(session, sleep=delays.append)
    assert client.profile() == {"historyId": "cursor"}
    assert delays == [1, 2, 4]
    assert all(call[2]["timeout"] == (10, 60) for call in session.calls)


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded"])
def test_gmail_403_quota_errors_wait_through_minute_window(reason):
    delays = []
    quota_error = {"error": {"errors": [{"reason": reason}]}}
    session = Session(
        [Response(403, quota_error) for _ in range(6)]
        + [Response(200, {"historyId": "cursor"})]
    )
    assert GmailClient(session, sleep=delays.append).profile() == {"historyId": "cursor"}
    assert sum(delays) >= 60
    assert max(delays) <= 32


@pytest.mark.parametrize(
    "data", [{"error": {"errors": [{"reason": "insufficientPermissions"}]}}, None]
)
def test_non_quota_403_read_is_not_retried(data):
    session = Session([Response(403, data)])
    with pytest.raises(GmailError) as error:
        GmailClient(session).profile()
    assert error.value.status == 403
    assert len(session.calls) == 1


def test_persistent_quota_error_has_bounded_retries():
    quota_error = {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
    session = Session([Response(403, quota_error) for _ in range(8)])
    delays = []
    with pytest.raises(GmailError):
        GmailClient(session, sleep=delays.append).profile()
    assert len(session.calls) == 8
    assert len(delays) == 7


@pytest.mark.parametrize(
    "response",
    [requests.Timeout(), Response(503), Response(408), Response(200, {"bad": "response"})],
)
def test_publish_uncertainty_is_never_retried(response):
    session = Session([response])
    client = GmailClient(session)
    with pytest.raises(PublishUncertain):
        client.create_draft("thread", "encoded")
    assert len(session.calls) == 1


def test_rejected_publish_is_definitive_and_has_no_email_in_error():
    session = Session([Response(403, {"error": "private body"})])
    with pytest.raises(GmailError) as error:
        GmailClient(session).create_draft("thread", "encoded")
    assert error.value.status == 403
    assert "private" not in str(error.value)


def test_large_inline_text_body_is_fetched_but_attachment_is_not():
    inline = {"mimeType": "text/plain", "body": {"attachmentId": "inline"}}
    attached = {"mimeType": "text/plain", "filename": "a.txt", "body": {"attachmentId": "file"}}
    source = {
        "id": "m",
        "threadId": "t",
        "internalDate": "123",
        "labelIds": ["INBOX"],
        "payload": {"mimeType": "multipart/mixed", "parts": [inline, attached]},
    }
    session = Session(
        [Response(200, {"messages": [source]}), Response(200, {"data": encoded("The actual text")})]
    )
    result = GmailClient(session).get_thread("t")
    assert result[0]["body_text"] == "The actual text"
    assert len(session.calls) == 2
    assert session.calls[1][1].endswith("/messages/m/attachments/inline")


def test_draft_index_paginates_and_resolves_missing_thread_id():
    session = Session(
        [
            Response(
                200, {"drafts": [{"id": "d1", "message": {"id": "m1"}}], "nextPageToken": "next"}
            ),
            Response(200, {"id": "d1", "message": {"id": "m1", "threadId": "t1"}}),
            Response(200, {"drafts": [{"id": "d2", "message": {"id": "m2", "threadId": "t2"}}]}),
        ]
    )
    drafts = GmailClient(session).list_drafts()
    assert [d["message"]["threadId"] for d in drafts] == ["t1", "t2"]
    assert session.calls[-1][2]["params"]["pageToken"] == "next"


def test_history_cursor_is_opaque_and_all_events_requested():
    session = Session([Response(200, {"historyId": "184467440737095516160"})])
    GmailClient(session).history_page("184467440737095516159")
    params = session.calls[0][2]["params"]
    assert params["startHistoryId"] == "184467440737095516159"
    assert "historyTypes" not in params
    assert "labelId" not in params
