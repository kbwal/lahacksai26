"""Export a read-only Gmail snapshot as local persona conversations and edit pairs.

    uv run python -m pipeline.gmail --db data/mail.db --adapter-name professional \
        --output data/training/gmail

The newest 10% of eligible threads are held out, including their edit feedback.
An adapter name is an explicit assignment, not an inferred relationship. Use
train.augment --relabel on the export for local model-assisted persona labeling.
"""

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path

from gmail_agent.client import is_automated
from gmail_agent.store import Store
from gmail_agent.text import comparable_body
from train.format import Conversation, Edit


def clean_text(text):
    """Conservatively remove standard quoted tails and signature delimiters."""
    lines = comparable_body(text).splitlines()
    kept = []
    for line in lines:
        if (
            re.match(r"^\s*On .+wrote:\s*$", line, re.IGNORECASE)
            or re.match(
                r"^\s*-{2,}\s*(Original Message|Forwarded message)\s*-*\s*$", line, re.IGNORECASE
            )
            or line in {"--", "-- "}
        ):
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return "\n".join(kept).strip()


def snapshot(store, account_id, adapter_name, test_fraction=0.1):
    """Read one SQLite snapshot; never change cached mail or contact the provider."""
    Conversation.from_dict({"adapter_name": adapter_name})
    if not 0 <= test_fraction < 1:
        raise ValueError("test_fraction must be at least zero and less than one")
    stats = Counter()
    conversations = []
    eligible_sent = set()
    with store.db:
        store.db.execute("BEGIN")
        account = store.account(account_id)
        threads = store.db.execute(
            "SELECT thread_id FROM threads WHERE account_id=? ORDER BY thread_id", (account_id,)
        ).fetchall()
        for thread in threads:
            messages = []
            for message in store.thread_messages(account_id, thread["thread_id"]):
                stats["messages_seen"] += 1
                if set(message["labels"]) & {"DRAFT", "SPAM", "TRASH"} or is_automated(message):
                    stats["messages_filtered"] += 1
                    continue
                text = clean_text(message["body_text"])
                if not text:
                    stats["empty_messages"] += 1
                    continue
                # Only Gmail's SENT label is trusted as evidence of user-authored targets.
                sender = "me" if "SENT" in message["labels"] else "them"
                messages.append({"from": sender, "text": text})
                if sender == "me":
                    eligible_sent.add(message["id"])
                    last_sent = len(messages)
                    timestamp = message["internal_date"]
            if not any(m["from"] == "me" for m in messages):
                stats["threads_without_targets"] += 1
                continue
            row = {
                "source": "gmail",
                "adapter_name": adapter_name,
                "account_id": account_id,
                "thread_id": thread["thread_id"],
                "timestamp_ms": timestamp,
                "conversation": messages[:last_sent],
            }
            Conversation.from_dict(row)
            conversations.append(row)

        conversations.sort(key=lambda r: (r["timestamp_ms"], r["thread_id"]))
        held_out = min(len(conversations) - 1, math.ceil(len(conversations) * test_fraction))
        test_threads = (
            {r["thread_id"] for r in conversations[-held_out:]} if held_out > 0 else set()
        )
        splits = {
            r["thread_id"]: "test" if r["thread_id"] in test_threads else "train"
            for r in conversations
        }
        for row in conversations:
            row["type"] = splits[row["thread_id"]]

        edits = []
        for feedback in store.feedback(account_id):
            if feedback["sent_message_id"] not in eligible_sent:
                stats["feedback_filtered"] += 1
                continue
            final = clean_text(feedback["final"])
            draft = clean_text(feedback["draft"])
            if not final or not draft:
                stats["feedback_filtered"] += 1
                continue
            # Keep the saved generation context; never add later messages to an edit pair.
            context = [{**m, "text": clean_text(m["text"])} for m in feedback["conversation"]]
            row = {
                "source": "gmail",
                "adapter_name": adapter_name
                if feedback["adapter_name"] == "default"
                else feedback["adapter_name"],
                "account_id": account_id,
                "thread_id": feedback["thread_id"],
                "sent_message_id": feedback["sent_message_id"],
                "conversation": [m for m in context if m["text"]],
                "draft": draft,
                "final": final,
                "type": splits[feedback["thread_id"]],
            }
            Edit.from_dict(row)
            edits.append(row)
        manifest = {
            "version": 1,
            "source": "gmail",
            "bootstrap": False,
            "adapter_name": adapter_name,
            "backfill_complete": bool(account["backfill_complete"]),
            "conversations": len(conversations),
            "edits": len(edits),
            "train_threads": len(conversations) - len(test_threads),
            "test_threads": len(test_threads),
            "split": "newest threads by last eligible sent message; feedback follows its thread",
            "counts": dict(stats),
        }
    return conversations, edits, manifest


def export(db_path, output, *, account_id=None, adapter_name, test_fraction=0.1):
    output = Path(output)
    store = Store(db_path, readonly=True)
    try:
        accounts = store.accounts()
        if account_id is None:
            if len(accounts) != 1:
                raise ValueError(
                    "Choose --account when the database does not have exactly one account"
                )
            account_id = accounts[0]["account_id"]
        conversations, edits, manifest = snapshot(
            store,
            account_id.lower(),
            adapter_name,
            test_fraction,
        )
    finally:
        store.close()
    if not conversations:
        raise ValueError(
            "No eligible sent messages. Import Gmail history before exporting training data."
        )
    # A new private directory prevents overwriting datasets or following existing output symlinks.
    output.mkdir(parents=True, mode=0o700)
    for name, rows in (("conversations.jsonl", conversations), ("edits.jsonl", edits)):
        path = output / name
        with path.open("x", encoding="utf-8") as target:
            path.chmod(0o600)
            for row in rows:
                target.write(json.dumps(row, ensure_ascii=False) + "\n")
    path = output / "manifest.json"
    with path.open("x", encoding="utf-8") as target:
        path.chmod(0o600)
        target.write(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--account")
    parser.add_argument("--adapter-name", required=True)
    parser.add_argument("--output", type=Path, required=True, help="new private output directory")
    parser.add_argument("--test-fraction", type=float, default=0.1)
    args = parser.parse_args()
    try:
        result = export(
            args.db,
            args.output,
            account_id=args.account,
            adapter_name=args.adapter_name,
            test_fraction=args.test_fraction,
        )
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
