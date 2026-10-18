"""Read-only provider clients with resumable channel/thread pagination."""

import math
import time
from urllib.parse import quote

import requests

from message_pipeline.models import Message, slack_time


class ProviderError(RuntimeError):
    def __init__(self, code):
        self.code = code
        # Provider bodies can contain private data. Only known codes reach logs.
        super().__init__("Message provider request failed")


class RateLimited(ProviderError):
    def __init__(self, seconds):
        super().__init__("rate_limited")
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = 60
        self.seconds = max(1, seconds) if math.isfinite(seconds) else 60


class ReadClient:
    def __init__(self, token, *, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"{self.auth_scheme} {token}"})

    def close(self):
        self.session.close()

    def get(self, path, **params):
        response = self.session.get(
            self.base_url + path, params=params, timeout=(10, 45), allow_redirects=False
        )
        if response.status_code == 429:
            delay = response.headers.get("Retry-After", 60)
            if self.source == "discord":
                try:
                    delay = response.json().get("retry_after", delay)
                except (ValueError, AttributeError):
                    pass
            raise RateLimited(delay)
        if response.status_code != 200:
            raise ProviderError(f"http_{response.status_code}")
        result = response.json()
        if self.source == "slack" and not result.get("ok"):
            code = result.get("error")
            if code == "ratelimited":
                raise RateLimited(response.headers.get("Retry-After", 60))
            raise ProviderError(code if code in {"invalid_cursor", "thread_not_found"} else "api")
        return result


class SlackClient(ReadClient):
    source = "slack"
    base_url = "https://slack.com/api/"
    auth_scheme = "Bearer"
    prunes_missing = True

    def identity(self):
        result = self.get("auth.test")
        if result.get("bot_id"):
            raise ValueError("Use a Slack user OAuth token to identify your own sent messages")
        return result["user_id"], result["team_id"]

    def validate_channel(self, channel_id, workspace_id):
        self.get("conversations.info", channel=channel_id)

    def verify_identity(self, account_id, workspace_id):
        if self.identity() != (account_id, workspace_id):
            raise ValueError("Slack token no longer matches the connected account/workspace")

    def initial_state(self, since_ms=None):
        seconds, micros = divmod(time.time_ns() // 1000, 1_000_000)
        return {"latest": f"{seconds}.{micros:06d}", "threads": []}

    def page(self, account_id, workspace_id, channel_id, state):
        state = {**state, "threads": list(state.get("threads", []))}
        threads = state["threads"]
        if threads:
            try:
                result = self.get(
                    "conversations.replies",
                    channel=channel_id,
                    ts=threads[0],
                    cursor=state.get("reply_cursor", ""),
                    latest=state["latest"],
                    limit=15,
                )
            except ProviderError as error:
                if error.code != "thread_not_found":
                    raise
                result = {"messages": []}
            cursor = self.next_cursor(result)
            if cursor:
                state["reply_cursor"] = cursor
            else:
                threads.pop(0)
                state.pop("reply_cursor", None)
        else:
            result = self.get(
                "conversations.history",
                channel=channel_id,
                latest=state["latest"],
                cursor=state.get("history_cursor", ""),
                limit=15,
            )
            cursor = self.next_cursor(result)
            state["history_cursor"] = cursor
            state["history_done"] = not cursor
            state["threads"] = list(
                dict.fromkeys(
                    item.get("thread_ts", item["ts"])
                    for item in result.get("messages", [])
                    if item.get("reply_count", 0) > 0
                )
            )
        messages = []
        for item in result.get("messages", []):
            if item.get("subtype") in {"channel_join", "channel_leave", "message_deleted"}:
                continue
            author = item.get("user") or item.get("bot_id", "")
            thread_id = item.get("thread_ts") or item["ts"]
            messages.append(
                Message(
                    source=self.source,
                    account_id=account_id,
                    workspace_id=workspace_id,
                    channel_id=channel_id,
                    thread_id=thread_id,
                    message_id=item["ts"],
                    author_id=author,
                    timestamp_ms=slack_time(item["ts"]),
                    text=item.get("text", ""),
                    is_self=author == account_id,
                    is_bot=bool(item.get("bot_id")) or item.get("subtype") == "bot_message",
                    reply_to_id=thread_id if thread_id != item["ts"] else None,
                )
            )
        done = state.get("history_done") and not state["threads"]
        return messages, None if done else state

    @staticmethod
    def next_cursor(result):
        cursor = result.get("response_metadata", {}).get("next_cursor", "").strip()
        if result.get("has_more") and not cursor:
            raise ProviderError("missing_cursor")
        return cursor


class DiscordClient(ReadClient):
    source = "discord"
    base_url = "https://discord.com/api/v10/"
    auth_scheme = "Bot"
    prunes_missing = False

    def identity(self):
        result = self.get("users/@me")
        if not result.get("bot"):
            raise ValueError("Discord ingestion requires an installed bot, not a user token")
        app = self.get("oauth2/applications/@me")
        if not app.get("flags", 0) & ((1 << 18) | (1 << 19)):
            raise ValueError("Enable the Discord bot's Message Content intent before importing")
        return result["id"]

    def verify_identity(self, account_id, workspace_id):
        # The selected human is not the bot; guild scope is checked per channel.
        self.identity()

    def validate_channel(self, channel_id, workspace_id):
        channel = self.get(f"channels/{quote(channel_id, safe='')}")
        if channel.get("guild_id") != workspace_id:
            raise ValueError("Discord channel must belong to the selected server")
        if channel.get("type") not in {0, 5, 10, 11, 12}:
            raise ValueError("Select a text channel or a specific thread, not a forum container")

    def initial_state(self, since_ms=None):
        if since_ms is not None:
            # Replay the last millisecond so snowflakes sharing that timestamp are retained.
            return {"after": str(max(0, ((since_ms - 1420070400000) << 22) - 1))}
        # Exclusive upper bound freezes this scan; newer arrivals enter the next scan.
        milliseconds = time.time_ns() // 1_000_000
        return {"before": str((milliseconds - 1420070400000) << 22)}

    def page(self, account_id, workspace_id, channel_id, state):
        direction = "after" if "after" in state else "before"
        items = self.get(
            f"channels/{quote(channel_id, safe='')}/messages", **{direction: state[direction]}, limit=100
        )
        messages = []
        for item in items:
            if item["channel_id"] != channel_id:
                raise ValueError("Discord returned a message from a different channel")
            # Keep ordinary human replies; omit system notices and thread starter stubs.
            if item.get("type", 0) not in {0, 19}:
                continue
            author = item.get("author", {})
            messages.append(
                Message(
                    source=self.source,
                    account_id=account_id,
                    workspace_id=workspace_id,
                    channel_id=channel_id,
                    thread_id=channel_id,
                    message_id=item["id"],
                    author_id=author.get("id", ""),
                    timestamp_ms=(int(item["id"]) >> 22) + 1420070400000,
                    text=item.get("content", ""),
                    is_self=author.get("id") == account_id,
                    is_bot=bool(author.get("bot")) or bool(item.get("webhook_id")),
                    reply_to_id=item.get("message_reference", {}).get("message_id"),
                )
            )
        # An empty page proves completion; a short page alone need not.
        if not items:
            return messages, None
        ids = [int(item["id"]) for item in items]
        cursor = max(ids) if direction == "after" else min(ids)
        if (
            direction == "after" and min(ids) <= int(state[direction])
            or direction == "before" and max(ids) >= int(state[direction])
        ):
            raise ProviderError("pagination_stalled")
        return messages, {direction: str(cursor)}


def client_for(source, token):
    return {"slack": SlackClient, "discord": DiscordClient}[source](token)
