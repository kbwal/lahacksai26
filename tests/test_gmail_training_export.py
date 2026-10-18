import json
import sqlite3

import pytest
from test_format import CharTokenizer
from test_gmail_worker import message

from gmail_agent.store import Store
from pipeline.gmail import clean_text, export, snapshot
from train.format import Conversation, Edit, masked, training_examples


@pytest.fixture
def mailbox(tmp_path):
    store = Store(tmp_path / "mail.db")
    with store.db:
        store.add_account("me@example.com", "unused")
        for i in range(4):
            thread = f"thread-{i}"
            store.replace_thread(
                "me@example.com",
                thread,
                [
                    message(f"in-{i}", thread, i * 10, body="Can you meet?"),
                    message(
                        f"sent-{i}",
                        thread,
                        i * 10 + 1,
                        ["SENT"],
                        body="Tuesday works.\n\nOn Monday, Friend wrote:\nCan you meet?",
                    ),
                    message(f"later-{i}", thread, i * 10 + 2, body="Future incoming"),
                ],
            )
        store.enqueue(
            "me@example.com",
            "thread-3",
            "in-3",
            {
                "adapter_name": "social",
                "conversation": [{"from": "them", "text": "Can you meet?"}],
            },
        )
        store.set_state(1, "sent", generated_text="No thanks.", sent_message_id="sent-3")
    yield store
    store.close()


def test_export_isolates_account_filters_and_splits_entire_threads(mailbox, tmp_path):
    with mailbox.db:
        mailbox.add_account("other@example.com", "unused")
        mailbox.replace_thread(
            "other@example.com",
            "thread-0",
            [
                message("private", "thread-0", 999, ["SENT"], "Other account content"),
            ],
        )
        newsletter = message("bulk", "bulk", 990, ["SENT"], "Newsletter")
        newsletter["headers"]["list-unsubscribe"] = "unsubscribe"
        spoof = message("spoof", "spoof", 999)
        spoof["headers"]["from"] = "me@example.com"
        for m in [newsletter, spoof, message("draft", "draft", 999, ["DRAFT"], "Unsent")]:
            mailbox.replace_thread("me@example.com", m["thread_id"], [m])
    out = tmp_path / "training"
    manifest = export(mailbox.path, out, account_id="me@example.com", adapter_name="professional")
    rows = [json.loads(line) for line in (out / "conversations.jsonl").read_text().splitlines()]
    assert (manifest["train_threads"], manifest["test_threads"], manifest["edits"]) == (3, 1, 1)
    assert manifest["bootstrap"] is False
    assert [r["type"] for r in rows] == ["train", "train", "train", "test"]
    assert all(r["source"] == "gmail" and len(r["conversation"]) == 2 for r in rows)
    assert all(r["conversation"][-1] == {"from": "me", "text": "Tuesday works."} for r in rows)
    feedback = json.loads((out / "edits.jsonl").read_text())
    assert feedback["type"] == "test"
    assert feedback["adapter_name"] == "social"
    assert feedback["conversation"] == [{"from": "them", "text": "Can you meet?"}]
    assert Edit.from_dict(feedback).final == "Tuesday works."
    for arm in ("base", "instruct"):
        for row in rows:
            examples, dropped = training_examples(
                CharTokenizer(), masked(Conversation.from_dict(row)), arm
            )
            assert examples and dropped == 0
            assert all(sum(mask) > 0 for _, mask in examples)
    assert out.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in out.iterdir())
    with pytest.raises(FileExistsError):
        export(mailbox.path, out, account_id="me@example.com", adapter_name="professional")
    with pytest.raises(ValueError, match="Choose --account"):
        export(mailbox.path, tmp_path / "ambiguous", adapter_name="professional")


def test_export_no_test_leakage_without_holdout(mailbox):
    rows, edits, manifest = snapshot(mailbox, "me@example.com", "professional", 0)
    assert all(r["type"] == "train" for r in rows + edits)
    assert manifest["test_threads"] == 0


def test_missing_or_empty_database_cannot_become_fake_training_data(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        export(missing, tmp_path / "out", adapter_name="professional")
    assert not missing.exists()
    store = Store(tmp_path / "empty.db")
    with store.db:
        store.add_account("me@example.com", "unused")
    store.close()
    with pytest.raises(ValueError, match="No eligible sent messages"):
        export(store.path, tmp_path / "out", adapter_name="professional")
    assert not (tmp_path / "out").exists()


def test_readonly_store_cannot_mutate_mail(mailbox):
    store = Store(mailbox.path, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.db.execute("DELETE FROM messages")
    finally:
        store.close()


@pytest.mark.parametrize("fraction", [-1, 1, float("nan"), float("inf")])
def test_invalid_split_rejected(mailbox, fraction):
    with pytest.raises(ValueError, match="test_fraction"):
        snapshot(mailbox, "me@example.com", "professional", fraction)


def test_cleaning_preserves_authored_text():
    assert clean_text("Hi\r\n\r\nSee you.\r\n-- \r\nSignature") == "Hi\n\nSee you."
    assert (
        clean_text("My answer\n> a quoted line\nAn inline answer") == "My answer\nAn inline answer"
    )
    assert clean_text("Hello\n-----Original Message-----\nOld text") == "Hello"
