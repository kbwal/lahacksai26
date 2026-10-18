import json

import pytest

from message_pipeline.__main__ import main
from message_pipeline.desktop import connections, inbox
from message_pipeline.models import Message
from message_pipeline.replies import generate_suggestions
from message_pipeline.store import Store


class Generator:
    version = "synthetic"
    adapter_name = "social"

    def generate(self, context):
        return "I can join at [TIME]."


def seed(store, source, workspace="22"):
    key = (source, "11", workspace)
    store.connect(key, "private-credential-reference", ["33"])
    store.save_page(key, "33", 1, [Message(
        source=source, account_id="11", workspace_id=workspace, channel_id="33",
        thread_id="33", message_id="44", author_id="55", timestamp_ms=1,
        text="Can you join?", is_self=False,
    )], None)
    generate_suggestions(store, key, Generator(), include_history=True)
    return key


@pytest.mark.parametrize("source", ["slack", "discord"])
def test_desktop_snapshot_retains_source_and_thread_without_credentials(tmp_path, source, capsys):
    store = Store(tmp_path / "messages.db")
    try:
        key = seed(store, source)
        result = inbox(store, key)
        draft = result["drafts"][0]
        assert draft["source"] == draft["appId"] == draft["chat"]["source"] == source
        assert draft["chat"]["threadId"] == "33"
        assert draft["chat"]["accountId"] == "11"
        assert draft["body"] == "I can join at [TIME]."
        assert draft["roleId"] == "social"
        assert "private-credential-reference" not in json.dumps(result)
        assert "private-credential-reference" not in json.dumps(connections(store, source))
        assert draft["externalUrl"].startswith(
            "https://discord.com/channels/22/33/44" if source == "discord"
            else "https://app.slack.com/client/22/33?thread_ts=33"
        )
        args = ["--source", source, "--db", str(store.path)]
        assert main([*args, "connections"]) == 0
        assert json.loads(capsys.readouterr().out)[0]["workspace_id"] == "22"
        assert main([*args, "--account", "11", "--workspace", "22", "inbox"]) == 0
        assert json.loads(capsys.readouterr().out)["drafts"][0]["id"] == draft["id"]
    finally:
        store.close()


def test_inbox_scopes_ids_and_excludes_failed_deselected_and_nonready_suggestions(tmp_path):
    store = Store(tmp_path / "messages.db")
    try:
        keys = [seed(store, "slack"), seed(store, "discord"), seed(store, "slack", "other")]
        assert len({inbox(store, key)["drafts"][0]["id"] for key in keys}) == 3
        key = keys[0]
        store.fail_channel(key, "33", "http_403")
        assert inbox(store, key)["drafts"] == []
        assert inbox(store, key)["status"]["channels"][0]["last_error"] == "http_403"
        store.connect(key, "test", ["other-channel"])
        assert inbox(store, key)["drafts"] == []
        store.connect(key, "test", ["33"])
        with store.db:
            store.db.execute("UPDATE suggestions SET state='replied' WHERE source='slack'")
        assert inbox(store, key)["drafts"] == []
        assert len(inbox(store, keys[1])["drafts"]) == 1
    finally:
        store.close()
