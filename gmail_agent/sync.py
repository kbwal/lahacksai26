"""Resumable backfill and transactional incremental mailbox synchronization."""

import json
import logging

from gmail_agent.client import GmailError
from gmail_agent.store import now_ms

log = logging.getLogger(__name__)


def backfill(store, client, account_id, *, query=None, max_pages=None, reset=False):
    account = store.account(account_id)
    if account["backfill_complete"] and not reset:
        return True
    if not account["backfill_anchor"] or reset:
        anchor = str(client.profile()["historyId"])
        with store.db:
            store.db.execute(
                """UPDATE accounts SET backfill_anchor=?,backfill_page=NULL,
                   backfill_query=?,backfill_complete=0,needs_reconcile=1,snapshot_id=snapshot_id+1
                   WHERE account_id=?""",
                (anchor, query if query is not None else account["backfill_query"], account_id),
            )
    elif query is not None and query != account["backfill_query"]:
        raise ValueError("Cannot change the history query during a resumable import")

    pages = 0
    while max_pages is None or pages < max_pages:
        account = store.account(account_id)
        try:
            page = client.threads_page(account["backfill_page"], account["backfill_query"])
        except GmailError as error:
            if error.status != 400 or not account["backfill_page"]:
                raise
            # Gmail page tokens can expire. Replaying earlier pages is harmless.
            with store.db:
                store.db.execute(
                    "UPDATE accounts SET backfill_page=NULL WHERE account_id=?", (account_id,)
                )
            continue
        threads = client.get_threads(item["id"] for item in page.get("threads", []))
        next_page = page.get("nextPageToken")
        with store.db:
            for thread_id, messages in threads.items():
                store.replace_thread(account_id, thread_id, messages)
            store.db.execute(
                "UPDATE accounts SET backfill_page=? WHERE account_id=?", (next_page, account_id)
            )
            if not next_page:
                store.db.execute(
                    """UPDATE messages SET deleted=1 WHERE account_id=? AND snapshot_id!=?""",
                    (account_id, account["snapshot_id"]),
                )
                store.db.execute(
                    """UPDATE accounts SET history_id=backfill_anchor,backfill_anchor=NULL,
                       backfill_complete=1 WHERE account_id=?""",
                    (account_id,),
                )
        pages += 1
        imported = store.db.execute(
            "SELECT COUNT(*) FROM threads WHERE account_id=?", (account_id,)
        ).fetchone()[0]
        log.info("Imported page %s: %s threads stored so far", pages, imported)
        if not next_page:
            return True
    return False


def _changes(store, client, account_id):
    cursor = store.account(account_id)["history_id"]
    affected = set()
    token = None
    while True:
        page = client.history_page(cursor, token)
        for history in page.get("history", []):
            changes = [
                change["message"]
                for key in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved")
                for change in history.get(key, [])
            ]
            # The generic field is useful if a provider omits specific change fields.
            for message in changes or history.get("messages", []):
                thread_id = message.get("threadId")
                if not thread_id:
                    row = store.db.execute(
                        "SELECT thread_id FROM messages WHERE account_id=? AND message_id=?",
                        (account_id, message["id"]),
                    ).fetchone()
                    thread_id = row[0] if row else None
                if thread_id:
                    affected.add(thread_id)
        token = page.get("nextPageToken")
        if not token:
            return affected, str(page["historyId"])


def sync_mailbox(store, client, account_id):
    from gmail_agent.replies import queue_candidates, reconcile

    was_backfilling = not store.account(account_id)["backfill_complete"]
    if was_backfilling:
        backfill(store, client, account_id)
    try:
        affected, cursor = _changes(store, client, account_id)
    except GmailError as error:
        if error.status != 404:
            raise
        backfill(store, client, account_id, reset=True)
        was_backfilling = True
        affected, cursor = _changes(store, client, account_id)

    drafts = client.list_drafts()
    # Refresh active threads even if a send raced with history pagination.
    affected.update(row["thread_id"] for row in store.active_drafts(account_id))
    previous_drafts = {
        row[0]: set(json.loads(row[1]))
        for row in store.db.execute(
            "SELECT thread_id,draft_ids FROM threads WHERE account_id=? AND draft_ids!='[]'",
            (account_id,),
        )
    }
    current_drafts = {}
    for draft in drafts:
        current_drafts.setdefault(draft["message"]["threadId"], set()).add(draft["id"])
    affected.update(
        thread_id
        for thread_id in previous_drafts.keys() | current_drafts.keys()
        if previous_drafts.get(thread_id, set()) != current_drafts.get(thread_id, set())
    )
    threads = client.get_threads(sorted(affected))
    if was_backfilling or store.account(account_id)["needs_reconcile"]:
        candidate_ids = {
            row[0]
            for row in store.db.execute(
                "SELECT thread_id FROM threads WHERE account_id=?", (account_id,)
            )
        } | affected
    else:
        candidate_ids = affected

    # Network work finishes before the transaction; any failure leaves the cursor intact.
    with store.db:
        for thread_id, messages in threads.items():
            store.replace_thread(account_id, thread_id, messages)
        store.set_draft_index(account_id, drafts)
        reconciled = reconcile(store, account_id)
        queued = queue_candidates(store, account_id, candidate_ids)
        store.db.execute(
            "UPDATE accounts SET history_id=?,last_sync_at=?,needs_reconcile=0 WHERE account_id=?",
            (cursor, now_ms(), account_id),
        )
    return {"threads_refreshed": len(threads), "queued": queued, "reconciled": reconciled}
