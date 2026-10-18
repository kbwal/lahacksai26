"""Local reviewable suggestions; these functions never publish provider messages."""

import json
import time

from gmail_agent.locking import AlreadyRunning, worker_lock

CONTEXT_MESSAGES = 3


def human_messages(store, key, channel_id, thread_id, *, last=CONTEXT_MESSAGES):
    return store.conversation(key, channel_id, thread_id, humans_only=True, last=last)


def context_for(messages):
    latest = messages[-1]
    return {
        "source": latest["source"],
        "recipient": latest["author_id"],
        "relationship": "unknown",
        "channel_id": latest["channel_id"],
        "thread_id": latest["thread_id"],
        "message_ids": [item["message_id"] for item in messages[-CONTEXT_MESSAGES:]],
        "conversation": [
            {"from": "me" if item["is_self"] else "them", "text": item["text"]}
            for item in messages[-CONTEXT_MESSAGES:]
        ],
        "thread": "\n\n".join(
            f"From: {item['author_id']}\n{item['text']}" for item in messages[-CONTEXT_MESSAGES:]
        ),
    }


def reconcile(store, key):
    """Caller holds the connection lock and transaction."""
    rows = store.db.execute(
        """SELECT suggestions.* FROM suggestions JOIN channels
           USING(source,account_id,workspace_id,channel_id)
           WHERE source=? AND account_id=? AND workspace_id=? AND selected=1
           AND channels.last_error IS NULL AND state IN ('queued','ready','failed')""",
        key,
    ).fetchall()
    for row in rows:
        messages = human_messages(store, key, row["channel_id"], row["thread_id"], last=1)
        observed = store.observed_reply(key, row)
        if observed:
            store.db.execute(
                "UPDATE suggestions SET state='replied',observed_reply_id=? WHERE id=?",
                (observed, row["id"]),
            )
        elif not messages or messages[-1]["message_id"] != row["trigger_message_id"]:
            store.db.execute("UPDATE suggestions SET state='superseded' WHERE id=?", (row["id"],))


def generate_suggestions(
    store, key, generator, *, channel_id=None, thread_id=None, limit=20, include_history=False
):
    if bool(channel_id) != bool(thread_id):
        raise ValueError("Select both channel_id and thread_id")
    if limit < 1:
        raise ValueError("limit must be positive")
    generated = 0
    failed = 0
    first_error = None
    with worker_lock(store.path, json.dumps(key)):
        connection = store.connection(key)
        with store.db:
            reconcile(store, key)
        if channel_id:
            if channel_id not in {row["channel_id"] for row in store.channels(key)}:
                raise ValueError("Channel is not selected; reconnect to select it")
            conversations = [(channel_id, thread_id)]
        else:
            conversations = store.db.execute(
                """SELECT channel_id,thread_id FROM messages JOIN channels
                   USING(source,account_id,workspace_id,channel_id)
                   WHERE source=? AND account_id=? AND workspace_id=? AND selected=1
                   AND channels.last_error IS NULL
                   GROUP BY channel_id,thread_id HAVING max(timestamp_ms)>=?
                   ORDER BY max(timestamp_ms) DESC""",
                (*key, 0 if include_history else connection["activated_at_ms"]),
            ).fetchall()
        fresh, retries = [], []
        for channel, thread in conversations:
            messages = human_messages(store, key, channel, thread)
            if not messages or messages[-1]["is_self"]:
                continue
            latest = messages[-1]
            if not include_history and latest["timestamp_ms"] < connection["activated_at_ms"]:
                continue
            scope = (*key, channel, thread, latest["message_id"])
            context = {**context_for(messages), "adapter_name": generator.adapter_name}
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
            # A failure with unchanged context is retried only after new work, so a
            # conversation that always fails can't take the whole budget every cycle.
            unchanged_failure = row and row["state"] == "failed" and row["context"] == encoded
            (retries if unchanged_failure else fresh).append((scope, context, encoded))
        for attempts, (scope, context, encoded) in enumerate(fresh + retries):
            if attempts >= limit:
                break  # Failures count too: each attempt can take seconds under the lock.
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
            except Exception as error:  # noqa: BLE001 -- isolate model failures per conversation
                with store.db:
                    store.db.execute(
                        "UPDATE suggestions SET state='failed',last_error=? WHERE id=?",
                        (type(error).__name__, suggestion_id),
                    )
                failed += 1
                if first_error is None:
                    first_error = error
    if first_error is not None and generated == 0:
        raise first_error
    return {"source": key[0], "generated": generated, "failed": failed}


def dismiss(store, key, suggestion_id):
    with worker_lock(store.path, json.dumps(key)), store.db:
        changed = store.db.execute(
            """UPDATE suggestions SET state='dismissed' WHERE id=? AND source=?
               AND account_id=? AND workspace_id=? AND state IN ('queued','ready','failed')""",
            (suggestion_id, *key),
        )
        if changed.rowcount != 1:
            raise ValueError("No active suggestion with that ID in this connection")
