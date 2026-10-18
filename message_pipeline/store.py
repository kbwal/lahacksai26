"""Independent chat checkpoints; Gmail's existing database remains authoritative."""

import json
import sqlite3
import time
from pathlib import Path

from message_pipeline.models import gmail_message

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    source TEXT NOT NULL CHECK(source IN ('slack','discord')),
    account_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    activated_at_ms INTEGER NOT NULL,
    retry_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY(source,account_id,workspace_id)
);
CREATE TABLE IF NOT EXISTS channels (
    source TEXT NOT NULL,
    account_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    cursor TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    last_complete_at REAL,
    selected INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    scan_started_ms INTEGER,
    PRIMARY KEY(source,account_id,workspace_id,channel_id),
    FOREIGN KEY(source,account_id,workspace_id)
        REFERENCES connections(source,account_id,workspace_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS messages (
    source TEXT NOT NULL,
    account_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(source,account_id,workspace_id,channel_id,message_id),
    FOREIGN KEY(source,account_id,workspace_id,channel_id)
        REFERENCES channels(source,account_id,workspace_id,channel_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS message_conversations
    ON messages(source,account_id,workspace_id,channel_id,thread_id,timestamp_ms,message_id);
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    account_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    trigger_message_id TEXT NOT NULL,
    context TEXT NOT NULL,
    generated_text TEXT,
    model_version TEXT,
    created_at_ms INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','ready','replied','superseded','dismissed','failed')),
    observed_reply_id TEXT,
    last_error TEXT,
    UNIQUE(source,account_id,workspace_id,channel_id,thread_id,trigger_message_id),
    FOREIGN KEY(source,account_id,workspace_id,channel_id)
        REFERENCES channels(source,account_id,workspace_id,channel_id) ON DELETE CASCADE
);
"""


class Disconnected(ValueError):
    """The connection or its credential was removed; a polling worker must exit."""


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
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(channels)")}
            for name, definition in (
                ("selected", "INTEGER NOT NULL DEFAULT 1"),
                ("last_error", "TEXT"),
                ("scan_started_ms", "INTEGER"),
            ):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE channels ADD COLUMN {name} {definition}")

    def close(self):
        self.db.close()

    def connect(self, key, credential_ref, channel_ids):
        if not channel_ids or any(not channel for channel in channel_ids):
            raise ValueError("Select at least one channel")
        with self.db:
            self.db.execute(
                """INSERT INTO connections(source,account_id,workspace_id,credential_ref,activated_at_ms)
                   VALUES (?,?,?,?,?) ON CONFLICT(source,account_id,workspace_id)
                   DO UPDATE SET credential_ref=excluded.credential_ref,retry_at=0,last_error=NULL""",
                (*key, credential_ref, time.time_ns() // 1_000_000),
            )
            self.db.execute(
                "UPDATE channels SET selected=0 WHERE source=? AND account_id=? AND workspace_id=?",
                key,
            )
            for channel_id in channel_ids:
                self.db.execute(
                    """INSERT INTO channels(source,account_id,workspace_id,channel_id)
                       VALUES (?,?,?,?) ON CONFLICT(source,account_id,workspace_id,channel_id)
                       DO UPDATE SET selected=1,last_error=NULL""",
                    (*key, channel_id),
                )

    def connection(self, key):
        row = self.db.execute(
            "SELECT * FROM connections WHERE source=? AND account_id=? AND workspace_id=?", key
        ).fetchone()
        if row is None:
            raise Disconnected("Connection not found; run connect first")
        return row

    def channels(self, key, *, selected_only=True):
        return self.db.execute(
            """SELECT * FROM channels WHERE source=? AND account_id=? AND workspace_id=?
               AND (?=0 OR selected=1) ORDER BY channel_id""",
            (*key, selected_only),
        ).fetchall()

    def fail_channel(self, key, channel_id, error, *, restart=False):
        with self.db:
            self.db.execute(
                """UPDATE channels SET last_error=?,cursor=CASE WHEN ? THEN NULL ELSE cursor END
                   WHERE source=? AND account_id=? AND workspace_id=? AND channel_id=?""",
                (error, restart, *key, channel_id),
            )

    def newest_timestamp(self, key, channel_id):
        return self.db.execute(
            """SELECT max(timestamp_ms) FROM messages WHERE source=? AND account_id=?
               AND workspace_id=? AND channel_id=?""",
            (*key, channel_id),
        ).fetchone()[0]

    def begin(self, key, channel_id, initial_state, started_ms):
        """Start a scan unless one is in progress; `started_ms` precedes its boundary."""
        scope = (*key, channel_id)
        with self.db:
            self.db.execute(
                """UPDATE channels SET cursor=?,generation=generation+1,scan_started_ms=? WHERE
                   source=? AND account_id=? AND workspace_id=? AND channel_id=? AND cursor IS NULL""",
                (json.dumps(initial_state), started_ms, *scope),
            )
        return self.db.execute(
            "SELECT * FROM channels WHERE source=? AND account_id=? AND workspace_id=? AND channel_id=?",
            scope,
        ).fetchone()

    def save_page(self, key, channel_id, generation, messages, next_state, *, prune=False):
        scope = (*key, channel_id)
        with self.db:
            for message in messages:
                if (
                    message.source,
                    message.account_id,
                    message.workspace_id,
                    message.channel_id,
                ) != scope:
                    raise ValueError("Message escaped the selected connection/channel")
                self.db.execute(
                    """INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source,account_id,workspace_id,channel_id,message_id)
                       DO UPDATE SET thread_id=excluded.thread_id,timestamp_ms=excluded.timestamp_ms,
                       generation=excluded.generation,payload=excluded.payload""",
                    (
                        *scope,
                        message.message_id,
                        message.thread_id,
                        message.timestamp_ms,
                        generation,
                        json.dumps(message.to_dict(), ensure_ascii=False),
                    ),
                )
            self.db.execute(
                """UPDATE channels SET cursor=?,last_error=NULL,last_complete_at=CASE WHEN ? THEN ?
                   ELSE last_complete_at END WHERE
                   source=? AND account_id=? AND workspace_id=? AND channel_id=?""",
                (
                    json.dumps(next_state) if next_state is not None else None,
                    next_state is None,
                    time.time(),
                    *scope,
                ),
            )
            # Only providers whose completed scan authoritatively reflects absence prune.
            if next_state is None and prune:
                self.db.execute(
                    """DELETE FROM messages WHERE source=? AND account_id=? AND workspace_id=?
                       AND channel_id=? AND generation!=?""",
                    (*scope, generation),
                )

    def export(self, key):
        self.connection(key)
        rows = self.db.execute(
            """SELECT payload FROM messages WHERE source=? AND account_id=? AND workspace_id=?
               ORDER BY channel_id,thread_id,timestamp_ms,message_id""",
            key,
        )
        for row in rows:
            yield {**json.loads(row[0]), "usage": "context_only"}

    def conversation(self, key, channel_id, thread_id, *, humans_only=False, last=None):
        filters = ""
        if humans_only:
            filters = """ AND json_extract(payload,'$.is_bot')=0
                AND trim(json_extract(payload,'$.text'), char(9,10,11,12,13,32))!=''"""
        order = "DESC" if last is not None else "ASC"
        rows = self.db.execute(
            f"""SELECT payload FROM messages WHERE source=? AND account_id=? AND workspace_id=?
               AND channel_id=? AND thread_id=? {filters}
               ORDER BY timestamp_ms {order},message_id {order} LIMIT ?""",
            (*key, channel_id, thread_id, last if last is not None else -1),
        )
        result = [json.loads(row[0]) for row in rows]
        return result[::-1] if last is not None else result

    def observed_reply(self, key, suggestion):
        # Only one result crosses the SQLite boundary, even for a busy channel.
        trigger = self.db.execute(
            """SELECT timestamp_ms,message_id FROM messages WHERE source=? AND account_id=?
               AND workspace_id=? AND channel_id=? AND message_id=?""",
            (*key, suggestion["channel_id"], suggestion["trigger_message_id"]),
        ).fetchone()
        row = self.db.execute(
            """SELECT message_id FROM messages WHERE source=? AND account_id=? AND workspace_id=?
               AND channel_id=? AND thread_id=? AND timestamp_ms>=?
               AND (timestamp_ms,message_id)>(?,?)
               AND json_extract(payload,'$.is_self')=1 AND json_extract(payload,'$.is_bot')=0
               AND trim(json_extract(payload,'$.text'), char(9,10,11,12,13,32))!=''
               ORDER BY timestamp_ms,message_id LIMIT 1""",
            (*key, suggestion["channel_id"], suggestion["thread_id"], suggestion["created_at_ms"],
             trigger["timestamp_ms"] if trigger else -1, trigger["message_id"] if trigger else ""),
        ).fetchone()
        return row[0] if row else None

    def suggestions(self, key):
        self.connection(key)
        for row in self.db.execute(
            """SELECT suggestions.* FROM suggestions JOIN channels
               USING(source,account_id,workspace_id,channel_id)
               WHERE source=? AND account_id=? AND workspace_id=? AND selected=1 ORDER BY id""",
            key,
        ):
            item = {**dict(row), "context": json.loads(row["context"]), "usage": "context_only"}
            item["observed_reply"] = None
            if row["observed_reply_id"]:
                reply = self.db.execute(
                    """SELECT payload FROM messages WHERE source=? AND account_id=? AND workspace_id=?
                       AND channel_id=? AND message_id=?""",
                    (*key, row["channel_id"], row["observed_reply_id"]),
                ).fetchone()
                if reply:
                    item["observed_reply"] = json.loads(reply[0])
            # Conversation proximity is not evidence that the suggestion was copied.
            item["suggestion_use_confirmed"] = False
            yield item

    def status(self, key):
        connection = self.connection(key)
        return {
            "source": key[0],
            "account_id": key[1],
            "workspace_id": key[2],
            "retry_at": connection["retry_at"],
            "last_error": connection["last_error"],
            "channels": [
                {
                    "channel_id": row["channel_id"],
                    "scan_in_progress": row["cursor"] is not None,
                    "last_complete_at": row["last_complete_at"],
                    "selected": bool(row["selected"]),
                    "last_error": row["last_error"],
                }
                for row in self.channels(key, selected_only=False)
            ],
            "messages": self.db.execute(
                "SELECT count(*) FROM messages WHERE source=? AND account_id=? AND workspace_id=?",
                key,
            ).fetchone()[0],
        }


def export_gmail(path, account_id):
    """Read a consistent existing mailbox snapshot without touching Gmail or its schema."""
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM accounts WHERE account_id=?", (account_id,)).fetchone():
            raise ValueError("Gmail account not found")
        for row in db.execute(
            """SELECT * FROM messages WHERE account_id=? AND deleted=0
               ORDER BY thread_id,internal_date,message_id""",
            (account_id,),
        ):
            message = {
                **dict(row),
                "id": row["message_id"],
                "headers": json.loads(row["headers"]),
                "labels": json.loads(row["labels"]),
            }
            if "DRAFT" not in message["labels"]:
                yield {**gmail_message(message, account_id).to_dict(), "usage": "context_only"}
    finally:
        db.close()
