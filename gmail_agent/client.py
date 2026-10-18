"""Gmail REST client; sending is only exposed to explicit desktop review."""

import base64
import logging
import time
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)


def is_rate_limited(response):
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    try:
        errors = response.json()["error"].get("errors", [])
        return any(
            error.get("reason") in {"rateLimitExceeded", "userRateLimitExceeded"}
            for error in errors
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


class GmailError(RuntimeError):
    def __init__(self, status, operation):
        self.status = status
        super().__init__(f"Gmail {operation} failed (HTTP {status})")


class PublishUncertain(RuntimeError):
    """A create request may have reached Gmail; never replay it automatically."""


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def decode_data(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def body_text(payload):
    """Prefer plain text in alternatives; ignore attachments and forwarded MIME parts."""
    mime = payload.get("mimeType", "")
    if payload.get("filename") or mime == "message/rfc822":
        return ""
    parts = payload.get("parts", [])
    if parts:
        if mime == "multipart/alternative":
            for part in sorted(parts, key=lambda p: p.get("mimeType") != "text/plain"):
                text = body_text(part)
                if text.strip():
                    return text
            return ""
        return "\n".join(filter(None, (body_text(part) for part in parts)))
    if mime not in {"text/plain", "text/html"}:
        return ""
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    content_type = EmailMessage()
    content_type["Content-Type"] = headers.get("content-type", mime)
    charset = content_type.get_content_charset() or "utf-8"
    data = decode_data(payload.get("body", {}).get("data", ""))
    try:
        text = data.decode(charset, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    if mime == "text/html":
        parser = _HTMLText()
        parser.feed(text)
        return "\n".join(
            line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
        )
    return text.strip()


def normalize_message(message):
    payload = message.get("payload", {})
    headers = {}
    for header in payload.get("headers", []):
        name = header["name"].lower()
        headers[name] = ", ".join(filter(None, [headers.get(name), header["value"]]))
    return {
        "id": message["id"],
        "thread_id": message["threadId"],
        "internal_date": int(message["internalDate"]),
        "labels": message.get("labelIds", []),
        "headers": headers,
        "body_text": body_text(payload),
    }


def reply_recipient(message):
    headers = message["headers"]
    addresses = getaddresses([headers.get("reply-to") or headers.get("from", "")])
    # A direct reply must have one unambiguous mailbox and no header injection.
    if len(addresses) != 1 or "@" not in addresses[0][1]:
        return None
    value = headers.get("reply-to") or headers.get("from", "")
    if "\r" in value or "\n" in value:
        return None
    return value


def is_automated(message):
    headers = message["headers"]
    sender = parseaddr(headers.get("from", ""))[1].lower()
    return (
        "list-unsubscribe" in headers
        or headers.get("precedence", "").lower() in {"bulk", "list", "junk"}
        or headers.get("auto-submitted", "no").lower() != "no"
        or any(part in sender for part in ("noreply", "no-reply", "no_reply", "donotreply"))
    )


def reply_raw(account_email, trigger, text):
    recipient = reply_recipient(trigger)
    parent_id = trigger["headers"].get("message-id")
    if not recipient or not parent_id:
        raise ValueError("Reply requires a recipient and an RFC Message-ID")
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = account_email
    message["To"] = recipient
    # Preserve the subject exactly; Gmail uses it alongside threadId and reply headers.
    message["Subject"] = trigger["headers"].get("subject", "")
    message["In-Reply-To"] = parent_id
    references = trigger["headers"].get("references", "").strip()
    message["References"] = f"{references} {parent_id}".strip()
    message.set_content(text)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


class GmailClient:
    base_url = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, session, sleep=time.sleep):
        self.session = session
        self.sleep = sleep

    def _get(self, path, **params):
        params = {key: value for key, value in params.items() if value is not None}
        for attempt in range(8):
            try:
                response = self.session.get(self.base_url + path, params=params, timeout=(10, 60))
            except requests.RequestException:
                if attempt == 7:
                    raise RuntimeError(
                        "Gmail connection failed; sync will resume next cycle"
                    ) from None
                self.sleep(min(2**attempt, 32))
                continue
            if (is_rate_limited(response) or response.status_code >= 500) and attempt < 7:
                delay = min(2**attempt, 32)
                log.warning("Gmail read throttled or unavailable; retrying in %ss", delay)
                self.sleep(delay)
                continue
            if not response.ok:
                raise GmailError(response.status_code, "read")
            return response.json()

    def profile(self):
        return self._get("/profile")

    def threads_page(self, page_token=None, query=""):
        return self._get(
            "/threads",
            pageToken=page_token,
            q=query or None,
            maxResults=100,
            includeSpamTrash="false",
        )

    def history_page(self, history_id, page_token=None):
        return self._get(
            "/history", startHistoryId=history_id, pageToken=page_token, maxResults=500
        )

    def _hydrate_body(self, message_id, payload):
        """Large text bodies can be stored as attachment IDs even without a filename."""
        if payload.get("filename") or payload.get("mimeType") == "message/rfc822":
            return
        body = payload.get("body", {})
        if payload.get("mimeType") in {"text/plain", "text/html"} and body.get("attachmentId"):
            attachment = self._get(
                f"/messages/{quote(message_id, safe='')}/attachments/"
                f"{quote(body['attachmentId'], safe='')}"
            )
            body["data"] = attachment["data"]
        for part in payload.get("parts", []):
            self._hydrate_body(message_id, part)

    def get_thread(self, thread_id):
        try:
            result = self._get(f"/threads/{quote(thread_id, safe='')}", format="full")
        except GmailError as error:
            if error.status == 404:
                return []
            raise
        messages = result.get("messages", [])
        for message in messages:
            self._hydrate_body(message["id"], message.get("payload", {}))
        return sorted(
            (normalize_message(m) for m in messages), key=lambda m: (m["internal_date"], m["id"])
        )

    def list_drafts(self):
        drafts = []
        token = None
        while True:
            result = self._get("/drafts", pageToken=token, maxResults=500)
            for draft in result.get("drafts", []):
                if not draft.get("message", {}).get("threadId"):
                    draft = self._get(f"/drafts/{quote(draft['id'], safe='')}", format="minimal")
                drafts.append(draft)
            token = result.get("nextPageToken")
            if not token:
                return drafts

    def get_draft(self, draft_id):
        draft = self._get(f"/drafts/{quote(draft_id, safe='')}", format="full")
        message = draft["message"]
        self._hydrate_body(message["id"], message.get("payload", {}))
        normalized = normalize_message(message)

        def attachments(payload):
            return (
                bool(payload.get("filename"))
                or payload.get("mimeType", "").startswith("image/")
                or any(attachments(p) for p in payload.get("parts", []))
            )

        normalized["has_attachments"] = attachments(message.get("payload", {}))
        return normalized

    def send_draft(self, draft_id, thread_id, raw):
        # The caller journals this attempt. Never retry a send after a timeout or 5xx.
        try:
            response = self.session.post(
                self.base_url + "/drafts/send",
                json={"id": draft_id, "message": {"threadId": thread_id, "raw": raw}},
                timeout=(10, 60),
            )
        except requests.RequestException:
            raise PublishUncertain("Send outcome unknown; check Gmail") from None
        if response.status_code >= 500 or response.status_code == 408:
            raise PublishUncertain("Send outcome unknown; check Gmail")
        if not response.ok:
            raise GmailError(response.status_code, "send")
        try:
            result = response.json()
            if not result.get("id") or result.get("threadId") != thread_id:
                raise ValueError
        except (ValueError, AttributeError):
            raise PublishUncertain("Send response was incomplete; check Gmail") from None
        return result

    def create_draft(self, thread_id, raw):
        # No automatic retries: Gmail has no create idempotency key.
        try:
            response = self.session.post(
                self.base_url + "/drafts",
                json={"message": {"threadId": thread_id, "raw": raw}},
                timeout=(10, 60),
            )
        except requests.RequestException:
            raise PublishUncertain(
                "Draft creation outcome unknown; reconcile before retrying"
            ) from None
        if response.status_code >= 500 or response.status_code == 408:
            raise PublishUncertain("Draft creation outcome unknown; reconcile before retrying")
        if not response.ok:
            raise GmailError(response.status_code, "draft creation")
        try:
            result = response.json()
            if not result.get("id") or result.get("message", {}).get("threadId") != thread_id:
                raise ValueError
        except (ValueError, AttributeError):
            raise PublishUncertain(
                "Draft response was incomplete; reconcile before retrying"
            ) from None
        return result
