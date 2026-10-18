"""Explicit desktop review and send; scheduled workers still never send mail."""

import hashlib
import json
import re
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from urllib.parse import quote

from gmail_agent.client import GmailError, PublishUncertain
from gmail_agent.store import now_ms

TOKEN = re.compile(r"\[[A-Z][A-Z0-9_ '-]*\]")


def snapshot(message):
    value = {
        key: message.get(key)
        for key in ("id", "thread_id", "headers", "body_text", "has_attachments")
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def inbox(store, client, account_id):
    drafts = []
    for row in store.active_drafts(account_id):
        if row["state"] != "published" or not row["gmail_draft_id"]:
            continue
        try:
            current = client.get_draft(row["gmail_draft_id"])
        except GmailError as error:
            if error.status == 404:
                continue
            raise
        if current["thread_id"] != row["thread_id"]:
            raise ValueError("Gmail returned a draft from a different thread")
        context = json.loads(row["context"])
        recipient, address = parseaddr(current["headers"].get("to", ""))
        drafts.append(
            {
                # The app caps IDs at 100 characters; an address can be longer.
                "id": f"gmail:{hashlib.sha256(account_id.encode()).hexdigest()[:16]}:{row['id']}",
                "appId": "gmail",
                "roleId": context.get("adapter_name", "professional"),
                "subject": current["headers"].get("subject", ""),
                "recipient": recipient or address,
                "address": current["headers"].get("to", ""),
                "context": str(context["conversation"][-1].get("text", ""))[:10000]
                if context.get("conversation")
                else "",
                "body": current["body_text"],
                "updatedAt": datetime.fromtimestamp(
                    current["internal_date"] / 1000, UTC
                ).isoformat(),
                "externalUrl": f"https://mail.google.com/mail/u/?authuser={quote(account_id, safe='')}#drafts",
                "delivery": {
                    "accountId": account_id,
                    "generationId": row["id"],
                    "gmailDraftId": row["gmail_draft_id"],
                    "snapshot": snapshot(current),
                },
            }
        )
    return {"accountId": account_id, "drafts": drafts}


def send(store, client, account_id, request):
    if not isinstance(request, dict):
        raise TypeError("Invalid send request")
    body = request.get("body")
    if not isinstance(body, str) or not body.strip() or len(body) > 60000 or TOKEN.search(body):
        raise ValueError("Fill every placeholder before sending a nonempty reply")
    row = store.draft(request.get("generationId"))
    if (
        row is None
        or row["account_id"] != account_id
        or row["gmail_draft_id"] != request.get("gmailDraftId")
    ):
        raise ValueError("This draft does not belong to the selected Gmail account")
    # Persist before making any mutating request. A crash or lost response never enables a replay.
    store.db.execute("""CREATE TABLE IF NOT EXISTS desktop_send_attempts (
        account_id TEXT NOT NULL, draft_id TEXT NOT NULL, state TEXT NOT NULL,
        message_id TEXT, PRIMARY KEY(account_id,draft_id))""")
    prior = store.db.execute(
        "SELECT * FROM desktop_send_attempts WHERE account_id=? AND draft_id=?",
        (account_id, row["gmail_draft_id"]),
    ).fetchone()
    if prior and prior["state"] == "sent":
        return {
            "status": "sent",
            "messageId": prior["message_id"],
            "message": "This draft was already sent in Gmail; no new message was sent.",
        }
    if prior and prior["state"] == "sending":
        return {
            "status": "uncertain",
            "message": "Delivery is unconfirmed. Check Gmail before taking further action; this draft will not be resent.",
        }
    if row["state"] != "published":
        raise ValueError("This draft is no longer available to send; refresh Gmail")
    current = client.get_draft(row["gmail_draft_id"])
    if current["thread_id"] != row["thread_id"] or snapshot(current) != request.get("snapshot"):
        raise ValueError("This draft changed in Gmail. Refresh and review the latest version")
    headers = current["headers"]
    recipients = getaddresses([headers.get("to", "")])
    if (
        len(recipients) != 1
        or "@" not in recipients[0][1]
        or any("\n" in v or "\r" in v for v in headers.values())
    ):
        raise ValueError("Open Gmail to review this draft's recipients")
    if current.get("has_attachments") or headers.get("cc") or headers.get("bcc"):
        raise ValueError("Send drafts with attachments, Cc or Bcc from Gmail")
    if parseaddr(headers.get("from", ""))[1].lower() != account_id.lower():
        raise ValueError("The draft sender does not match the selected Gmail account")
    mime = EmailMessage(policy=policy.SMTP)
    for name in ("From", "To", "Subject", "In-Reply-To", "References"):
        if headers.get(name.lower()):
            mime[name] = headers[name.lower()]
    mime.set_content(body)
    import base64

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    # BEGIN IMMEDIATE makes the claim atomic across two simultaneous app processes.
    store.db.execute("BEGIN IMMEDIATE")
    try:
        prior = store.db.execute(
            "SELECT state FROM desktop_send_attempts WHERE account_id=? AND draft_id=?",
            (account_id, row["gmail_draft_id"]),
        ).fetchone()
        if prior and prior["state"] != "failed":
            raise ValueError("This draft already has a send attempt; refresh Gmail")
        store.db.execute(
            "INSERT INTO desktop_send_attempts VALUES (?,?, 'sending',NULL) ON CONFLICT(account_id,draft_id) DO UPDATE SET state='sending',message_id=NULL",
            (account_id, row["gmail_draft_id"]),
        )
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise
    try:
        result = client.send_draft(row["gmail_draft_id"], row["thread_id"], raw)
    except GmailError:
        with store.db:
            store.db.execute(
                "UPDATE desktop_send_attempts SET state='failed' WHERE account_id=? AND draft_id=?",
                (account_id, row["gmail_draft_id"]),
            )
        raise
    except PublishUncertain:
        return {
            "status": "uncertain",
            "message": "Delivery is unconfirmed. Check Gmail before taking further action; this draft will not be resent.",
        }
    with store.db:
        store.db.execute(
            "UPDATE desktop_send_attempts SET state='sent',message_id=? WHERE account_id=? AND draft_id=?",
            (result["id"], account_id, row["gmail_draft_id"]),
        )
        store.set_state(row["id"], "sent", sent_message_id=result["id"], last_error=None)
        # Save the actual final text for the infra feedback pipeline; the next sync replaces it.
        store.db.execute(
            """INSERT OR IGNORE INTO messages
            (account_id,message_id,thread_id,internal_date,labels,headers,body_text,snapshot_id)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                account_id,
                result["id"],
                row["thread_id"],
                now_ms(),
                '["SENT"]',
                json.dumps(headers),
                body,
                store.account(account_id)["snapshot_id"],
            ),
        )
    return {"status": "sent", "messageId": result["id"]}
