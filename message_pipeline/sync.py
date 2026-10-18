"""Checkpoint each page and respect provider backoff across worker restarts."""

import json
import time

from gmail_agent.locking import worker_lock
from message_pipeline.clients import ProviderError, RateLimited


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
            if key[0] == "slack":
                if client.identity() != (key[1], key[2]):
                    raise ValueError(
                        "Slack token no longer matches the connected account/workspace"
                    )
            else:
                client.identity()
            for channel in store.channels(key):
                channel_id = channel["channel_id"]
                client.validate_channel(channel_id, key[2])
                row = store.begin(key, channel_id, client.initial_state())
                state = json.loads(row["cursor"])
                for _ in range(max_pages):
                    try:
                        messages, next_state = client.page(key[1], key[2], channel_id, state)
                    except ProviderError as error:
                        if error.code == "invalid_cursor":
                            # Retain cached data, restart the scan on the next cycle.
                            with store.db:
                                store.db.execute(
                                    """UPDATE channels SET cursor=NULL WHERE source=? AND account_id=?
                                       AND workspace_id=? AND channel_id=?""",
                                    (*key, channel_id),
                                )
                        raise
                    if next_state == state:
                        raise ProviderError("pagination_stalled")
                    store.save_page(key, channel_id, row["generation"], messages, next_state)
                    if next_state is None:
                        break
                    state = next_state
            with store.db:
                from message_pipeline.replies import reconcile

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
