"""Checkpoint each page and respect provider backoff across worker restarts."""

import json
import time

from gmail_agent.locking import worker_lock
from message_pipeline.clients import ProviderError, RateLimited
from message_pipeline.replies import reconcile


def sync_channel(store, client, key, channel, max_pages):
    channel_id = channel["channel_id"]
    client.validate_channel(channel_id, key[2])
    since_ms = None
    if channel["last_complete_at"] is not None:
        since_ms = store.newest_timestamp(key, channel_id) or 0
    row = store.begin(key, channel_id, client.initial_state(since_ms))
    state = json.loads(row["cursor"])
    for _ in range(max_pages):
        messages, next_state = client.page(key[1], key[2], channel_id, state)
        if next_state == state:
            raise ProviderError("pagination_stalled")
        store.save_page(
            key, channel_id, row["generation"], messages, next_state, prune=client.prunes_missing
        )
        if next_state is None:
            break
        state = next_state


def sync_once(store, client, key, *, max_pages=100, credential_check=None):
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    with worker_lock(store.path, json.dumps(key)):
        connection = store.connection(key)
        if credential_check is not None:
            credential_check(connection)
        if connection["retry_at"] > time.time():
            return {**store.status(key), "deferred": True}
        try:
            # Recheck identity every cycle so a replaced/revoked token cannot mix workspaces.
            client.verify_identity(key[1], key[2])
            for channel in store.channels(key):
                channel_id = channel["channel_id"]
                try:
                    sync_channel(store, client, key, channel, max_pages)
                except RateLimited:
                    raise  # Backoff applies to this connection, not just the channel.
                except (ProviderError, ValueError) as error:
                    store.fail_channel(
                        key, channel_id, type(error).__name__,
                        restart=isinstance(error, ProviderError) and error.code == "invalid_cursor",
                    )
            with store.db:
                reconcile(store, key)
                store.db.execute(
                    """UPDATE connections SET last_error=NULL,retry_at=0
                       WHERE source=? AND account_id=? AND workspace_id=?""",
                    key,
                )
        except Exception as error:
            with store.db:
                store.db.execute(
                    """UPDATE connections SET last_error=?,retry_at=?
                       WHERE source=? AND account_id=? AND workspace_id=?""",
                    (
                        type(error).__name__,
                        time.time() + error.seconds if isinstance(error, RateLimited) else 0,
                        *key,
                    ),
                )
            if not isinstance(error, RateLimited):
                raise
            return {**store.status(key), "deferred": True}
        return {**store.status(key), "deferred": False}
