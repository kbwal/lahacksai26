"""Local reviewable suggestions; these functions never publish provider messages."""

import json
import time

from gmail_agent.locking import AlreadyRunning, worker_lock


def human_messages(store, key, channel_id, thread_id):
    return [
        item
        for item in store.conversation(key, channel_id, thread_id)
        if not item["is_bot"] and item["text"].strip()
    ]


def context_for(messages):
    latest = messages[-1]
    return {
        "source": latest["source"],
        "recipient": latest["author_id"],
        "relationship": "unknown",
        "channel_id": latest["channel_id"],
        "thread_id": latest["thread_id"],
        "message_ids": [item["message_id"] for item in messages[-3:]],
        "conversation": [
            {"from": "me" if item["is_self"] else "them", "text": item["text"]}
            for item in messages[-3:]
        ],
        "thread": "\n\n".join(
            f"From: {item['author_id']}\n{item['text']}" for item in messages[-3:]
        ),
    }


def reconcile(store, key):
    """Caller holds the connection lock and transaction."""
    rows = store.db.execute(
        """SELECT * FROM suggestions WHERE source=? AND account_id=? AND workspace_id=?
           AND state IN ('queued','ready','failed')""",
        key,
    ).fetchall()
    for row in rows:
        messages = human_messages(store, key, row["channel_id"], row["thread_id"])
        trigger_index = next(
            (
                index
                for index, item in enumerate(messages)
                if item["message_id"] == row["trigger_message_id"]
            ),
            None,
        )
        observed = next(
            (
                item
                for item in messages[(trigger_index + 1) if trigger_index is not None else 0 :]
                if item["is_self"] and item["timestamp_ms"] >= row["created_at_ms"]
            ),
            None,
        )
        if observed:
            store.db.execute(
                "UPDATE suggestions SET state='replied',observed_reply_id=? WHERE id=?",
                (observed["message_id"], row["id"]),
            )
        elif not messages or messages[-1]["message_id"] != row["trigger_message_id"]:
            store.db.execute("UPDATE suggestions SET state='superseded' WHERE id=?", (row["id"],))


def generate_suggestions(
    store, key, generator, *, channel_id=None, thread_id=None, limit=20, include_history=False
):
    if bool(channel_id) != bool(thread_id):
        raise ValueError("Select both channel_id and thread_id")
    generated = 0
    with worker_lock(store.path, json.dumps(key)):
        connection = store.connection(key)
        with store.db:
            reconcile(store, key)
        if channel_id:
            conversations = [(channel_id, thread_id)]
        else:
            conversations = store.db.execute(
                """SELECT channel_id,thread_id FROM messages
                   WHERE source=? AND account_id=? AND workspace_id=?
                   GROUP BY channel_id,thread_id ORDER BY max(timestamp_ms) DESC""",
                key,
            ).fetchall()
        for channel, thread in conversations:
            messages = human_messages(store, key, channel, thread)
            if not messages or messages[-1]["is_self"]:
                continue
            latest = messages[-1]
            if not include_history and latest["timestamp_ms"] < connection["activated_at_ms"]:
                continue
            scope = (*key, channel, thread, latest["message_id"])
            context = context_for(messages)
            encoded = json.dumps(context, sort_keys=True)
            row = store.db.execute(
                """SELECT * FROM suggestions WHERE source=? AND account_id=? AND workspace_id=?
                   AND channel_id=? AND thread_id=? AND trigger_message_id=?""",
                scope,
            ).fetchone()
            if row and (
                row["state"] in {"dismissed", "replied"}
                or row["state"] == "ready"
                and row["context"] == encoded
            ):
                continue
            with store.db:
                # Only machine suggestions are stored here. User composition remains in
                # the desktop app/provider; refresh a suggestion when cached context changes.
                store.db.execute(
                    """INSERT INTO suggestions(source,account_id,workspace_id,channel_id,thread_id,
                       trigger_message_id,context,created_at_ms,state) VALUES (?,?,?,?,?,?,?,?,'queued')
                       ON CONFLICT(source,account_id,workspace_id,channel_id,thread_id,trigger_message_id)
                       DO UPDATE SET context=excluded.context,state='queued',generated_text=NULL,
                       model_version=NULL,last_error=NULL,created_at_ms=excluded.created_at_ms""",
                    (*scope, encoded, time.time_ns() // 1_000_000),
                )
                suggestion_id = store.db.execute(
                    """SELECT id FROM suggestions WHERE source=? AND account_id=? AND workspace_id=?
                       AND channel_id=? AND thread_id=? AND trigger_message_id=?""",
                    scope,
                ).fetchone()[0]
            try:
                text = generator.generate(context)
                if not text or not text.strip():
                    raise ValueError("Model returned an empty suggestion")
                with store.db:
                    store.db.execute(
                        """UPDATE suggestions SET state='ready',generated_text=?,model_version=?
                           WHERE id=?""",
                        (text, generator.version, suggestion_id),
                    )
                generated += 1
            except AlreadyRunning:
                break  # The Gmail worker/trainer owns MLX; keep this suggestion queued.
            except Exception as error:
                with store.db:
                    store.db.execute(
                        "UPDATE suggestions SET state='failed',last_error=? WHERE id=?",
                        (type(error).__name__, suggestion_id),
                    )
                raise
            if generated >= limit:
                break
    return {"source": key[0], "generated": generated}


def dismiss(store, key, suggestion_id):
    with worker_lock(store.path, json.dumps(key)), store.db:
        changed = store.db.execute(
            """UPDATE suggestions SET state='dismissed' WHERE id=? AND source=?
               AND account_id=? AND workspace_id=? AND state IN ('queued','ready','failed')""",
            (suggestion_id, *key),
        )
        if changed.rowcount != 1:
            raise ValueError("No active suggestion with that ID in this connection")
