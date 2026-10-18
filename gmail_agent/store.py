"""Durable mailbox snapshots and reply turns. Mutations commit at the caller's boundary."""

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    activated_at INTEGER NOT NULL,
    history_id TEXT,
    backfill_anchor TEXT,
    backfill_page TEXT,
    backfill_query TEXT NOT NULL DEFAULT '',
    backfill_complete INTEGER NOT NULL DEFAULT 0,
    needs_reconcile INTEGER NOT NULL DEFAULT 1,
    snapshot_id INTEGER NOT NULL DEFAULT 0,
    last_sync_at INTEGER
);
CREATE TABLE IF NOT EXISTS threads (
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    thread_id TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    draft_ids TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (account_id, thread_id)
);
CREATE TABLE IF NOT EXISTS messages (
    account_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    internal_date INTEGER NOT NULL,
    labels TEXT NOT NULL,
    headers TEXT NOT NULL,
    body_text TEXT NOT NULL,
    cleaned_body TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    snapshot_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, message_id),
    FOREIGN KEY (account_id, thread_id) REFERENCES threads(account_id, thread_id)
);
CREATE INDEX IF NOT EXISTS messages_thread
    ON messages(account_id, thread_id, internal_date, message_id);
CREATE TABLE IF NOT EXISTS reply_drafts (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    trigger_message_id TEXT NOT NULL,
    context TEXT NOT NULL,
    generated_text TEXT,
    model_version TEXT,
    gmail_draft_id TEXT,
    state TEXT NOT NULL CHECK (state IN
        ('queued', 'ready', 'publishing', 'published', 'sent', 'dismissed', 'superseded', 'failed')),
    created_at INTEGER NOT NULL,
    published_at INTEGER,
    sent_message_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    UNIQUE (account_id, thread_id, trigger_message_id),
    FOREIGN KEY (account_id, thread_id) REFERENCES threads(account_id, thread_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_draft_per_thread
    ON reply_drafts(account_id, thread_id)
    WHERE state IN ('queued', 'ready', 'publishing', 'published');
CREATE UNIQUE INDEX IF NOT EXISTS one_generation_per_sent_message
    ON reply_drafts(account_id, sent_message_id) WHERE sent_message_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS worker_health (
    account_id TEXT PRIMARY KEY REFERENCES accounts(account_id),
    last_attempt_at INTEGER NOT NULL,
    last_success_at INTEGER,
    last_error TEXT,
    state TEXT NOT NULL DEFAULT 'running' CHECK (state IN ('running','idle','error'))
);
"""


def now_ms():
    return time.time_ns() // 1_000_000


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def account(self, account_id):
        row = self.db.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            raise ValueError("Account is not connected; run connect first")
        return row

    def accounts(self):
        return self.db.execute("SELECT * FROM accounts ORDER BY email").fetchall()

    def add_account(self, email, credential_ref, activated_at=None):
        account_id = email.lower()
        self.db.execute(
            """INSERT INTO accounts(account_id,email,credential_ref,activated_at) VALUES (?,?,?,?)
               ON CONFLICT(account_id) DO UPDATE SET credential_ref=excluded.credential_ref""",
            (
                account_id,
                email,
                credential_ref,
                activated_at if activated_at is not None else now_ms(),
            ),
        )
        return account_id

    def ensure_thread(self, account_id, thread_id):
        self.db.execute(
            "INSERT OR IGNORE INTO threads(account_id,thread_id) VALUES (?,?)",
            (account_id, thread_id),
        )

    def replace_thread(self, account_id, thread_id, messages):
        self.ensure_thread(account_id, thread_id)
        self.db.execute(
            "UPDATE messages SET deleted=1 WHERE account_id=? AND thread_id=?",
            (account_id, thread_id),
        )
        snapshot_id = self.account(account_id)["snapshot_id"]
        for message in messages:
            if message["thread_id"] != thread_id:
                raise ValueError("Gmail returned a message from a different thread")
            self.db.execute(
                """INSERT INTO messages
                   (account_id,message_id,thread_id,internal_date,labels,headers,body_text,snapshot_id)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id,message_id) DO UPDATE SET
                   thread_id=excluded.thread_id, internal_date=excluded.internal_date,
                   labels=excluded.labels, headers=excluded.headers, body_text=excluded.body_text,
                   deleted=0, snapshot_id=excluded.snapshot_id""",
                (
                    account_id,
                    message["id"],
                    thread_id,
                    message["internal_date"],
                    json.dumps(message["labels"]),
                    json.dumps(message["headers"]),
                    message["body_text"],
                    snapshot_id,
                ),
            )
        if messages:
            self.db.execute(
                "UPDATE threads SET subject=? WHERE account_id=? AND thread_id=?",
                (messages[-1]["headers"].get("subject", ""), account_id, thread_id),
            )

    def thread_messages(self, account_id, thread_id):
        rows = self.db.execute(
            """SELECT * FROM messages WHERE account_id=? AND thread_id=? AND deleted=0
               ORDER BY internal_date,message_id""",
            (account_id, thread_id),
        ).fetchall()
        return [self.decode_message(row) for row in rows]

    @staticmethod
    def decode_message(row):
        return {
            **dict(row),
            "id": row["message_id"],
            "labels": json.loads(row["labels"]),
            "headers": json.loads(row["headers"]),
        }

    def set_draft_index(self, account_id, drafts):
        self.db.execute("UPDATE threads SET draft_ids='[]' WHERE account_id=?", (account_id,))
        by_thread = {}
        for draft in drafts:
            by_thread.setdefault(draft["message"]["threadId"], []).append(draft["id"])
        for thread_id, ids in by_thread.items():
            self.ensure_thread(account_id, thread_id)
            self.db.execute(
                "UPDATE threads SET draft_ids=? WHERE account_id=? AND thread_id=?",
                (json.dumps(ids), account_id, thread_id),
            )

    def draft_ids(self, account_id, thread_id):
        row = self.db.execute(
            "SELECT draft_ids FROM threads WHERE account_id=? AND thread_id=?",
            (account_id, thread_id),
        ).fetchone()
        return json.loads(row[0]) if row else []

    def active_drafts(self, account_id):
        return self.db.execute(
            """SELECT * FROM reply_drafts WHERE account_id=?
               AND state IN ('queued','ready','publishing','published') ORDER BY id""",
            (account_id,),
        ).fetchall()

    def draft(self, draft_id):
        return self.db.execute("SELECT * FROM reply_drafts WHERE id=?", (draft_id,)).fetchone()

    def enqueue(self, account_id, thread_id, trigger_id, context):
        result = self.db.execute(
            """INSERT OR IGNORE INTO reply_drafts
               (account_id,thread_id,trigger_message_id,context,state,created_at)
               VALUES (?,?,?,?,'queued',?)""",
            (account_id, thread_id, trigger_id, json.dumps(context), now_ms()),
        )
        return result.rowcount == 1

    def set_state(self, draft_id, state, **fields):
        allowed = {
            "generated_text",
            "model_version",
            "gmail_draft_id",
            "published_at",
            "sent_message_id",
            "attempts",
            "last_error",
            "context",
        }
        if fields.keys() - allowed:
            raise ValueError("Unknown draft field")
        columns = ["state=?"] + [f"{key}=?" for key in fields]
        self.db.execute(
            f"UPDATE reply_drafts SET {','.join(columns)} WHERE id=?",
            (state, *fields.values(), draft_id),
        )

    def status(self, account_id):
        account = self.account(account_id)
        counts = dict(
            self.db.execute(
                "SELECT state,count(*) FROM reply_drafts WHERE account_id=? GROUP BY state",
                (account_id,),
            ).fetchall()
        )
        errors = [
            dict(row)
            for row in self.db.execute(
                """SELECT id,thread_id,state,last_error FROM reply_drafts
               WHERE account_id=? AND last_error IS NOT NULL ORDER BY id""",
                (account_id,),
            )
        ]
        health = self.db.execute(
            """SELECT last_attempt_at,last_success_at,last_error,state
               FROM worker_health WHERE account_id=?""",
            (account_id,),
        ).fetchone()
        return {
            "account": account["email"],
            "backfill_complete": bool(account["backfill_complete"]),
            "history_id": account["history_id"],
            "last_sync_at": account["last_sync_at"],
            "messages": self.db.execute(
                "SELECT count(*) FROM messages WHERE account_id=? AND deleted=0",
                (account_id,),
            ).fetchone()[0],
            "drafts": counts,
            "errors": errors,
            "worker": dict(health) if health else None,
        }

    def start_cycle(self, account_id):
        self.db.execute(
            """INSERT INTO worker_health(account_id,last_attempt_at) VALUES (?,?)
               ON CONFLICT(account_id) DO UPDATE SET
               last_attempt_at=excluded.last_attempt_at,state='running'""",
            (account_id, now_ms()),
        )

    def finish_cycle(self, account_id, error=None):
        if error is None:
            self.db.execute(
                """UPDATE worker_health SET last_success_at=?,last_error=NULL,state='idle'
                   WHERE account_id=?""",
                (now_ms(), account_id),
            )
        else:
            self.db.execute(
                "UPDATE worker_health SET last_error=?,state='error' WHERE account_id=?",
                (type(error).__name__, account_id),
            )

    def feedback(self, account_id):
        rows = self.db.execute(
            """SELECT d.*,m.body_text AS final_text FROM reply_drafts d
               JOIN messages m ON m.account_id=d.account_id AND m.message_id=d.sent_message_id
               WHERE d.account_id=? AND d.state='sent' ORDER BY d.id""",
            (account_id,),
        )
        for row in rows:
            yield {
                "account_id": account_id,
                "thread_id": row["thread_id"],
                "context": json.loads(row["context"]),
                "generated_text": row["generated_text"],
                "final_text": row["final_text"],
                "sent_message_id": row["sent_message_id"],
                "model_version": row["model_version"],
                "changed": row["generated_text"].strip() != row["final_text"].strip(),
            }
