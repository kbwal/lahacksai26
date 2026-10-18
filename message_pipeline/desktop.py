"""Small, credential-free snapshots for the native desktop UI."""

import hashlib
import json
from datetime import UTC, datetime
from urllib.parse import quote


def connections(store, source):
    return [
        store.status((source, row["account_id"], row["workspace_id"]))
        for row in store.db.execute(
            "SELECT account_id,workspace_id FROM connections WHERE source=? ORDER BY workspace_id,account_id",
            (source,),
        )
    ]


def inbox(store, key):
    status = store.status(key)
    drafts = []
    rows = store.db.execute(
        """SELECT suggestions.* FROM suggestions JOIN channels
           USING(source,account_id,workspace_id,channel_id)
           WHERE source=? AND account_id=? AND workspace_id=? AND selected=1
           AND channels.last_error IS NULL AND state='ready'
           ORDER BY created_at_ms DESC,id DESC LIMIT 500""",
        key,
    )
    for row in rows:
        context = json.loads(row["context"])
        body = row["generated_text"] or ""
        if not body.strip() or len(body) > 60000:
            continue
        source, account, workspace = key
        channel, thread = row["channel_id"], row["thread_id"]
        identity = json.dumps([*key, channel, thread, row["trigger_message_id"]])
        if source == "discord":
            url = f"https://discord.com/channels/{quote(workspace, safe='')}/{quote(channel, safe='')}/{quote(row['trigger_message_id'], safe='')}"
        else:
            url = f"https://app.slack.com/client/{quote(workspace, safe='')}/{quote(channel, safe='')}?thread_ts={quote(thread, safe='')}"
        conversation = context.get("conversation") or []
        drafts.append({
            "id": f"{source}:{hashlib.sha256(identity.encode()).hexdigest()}",
            "appId": source,
            "source": source,
            "roleId": context.get("adapter_name", "professional"),
            "subject": f"Reply in #{channel}",
            "recipient": context.get("recipient") or "Conversation",
            "address": f"#{channel} · {workspace}",
            "context": str(conversation[-1].get("text", ""))[:10000] if conversation else "",
            "body": body,
            "updatedAt": datetime.fromtimestamp(row["created_at_ms"] / 1000, UTC).isoformat(),
            "externalUrl": url,
            "chat": {
                "source": source, "accountId": account, "workspaceId": workspace,
                "channelId": channel, "threadId": thread,
                "suggestionId": row["id"], "stale": False,
            },
        })
    return {"source": key[0], "accountId": key[1], "workspaceId": key[2],
            "status": status, "drafts": drafts}
