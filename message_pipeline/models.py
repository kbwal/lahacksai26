"""The common message contract. Provider IDs are opaque strings, never floats."""

from dataclasses import asdict, dataclass
from decimal import Decimal
from email.utils import parseaddr

from message_pipeline import SOURCES


@dataclass(frozen=True)
class Message:
    source: str
    account_id: str
    workspace_id: str
    channel_id: str
    thread_id: str
    message_id: str
    author_id: str
    timestamp_ms: int
    text: str
    is_self: bool
    is_bot: bool = False
    reply_to_id: str | None = None

    def __post_init__(self):
        if self.source not in SOURCES:
            raise ValueError("Unknown message source")
        for name in ("account_id", "channel_id", "thread_id", "message_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a nonempty string")
        if not isinstance(self.timestamp_ms, int) or self.timestamp_ms < 0:
            raise ValueError("timestamp_ms must be a nonnegative integer")

    def to_dict(self):
        return {"schema_version": 1, **asdict(self)}


def slack_time(value):
    return int(Decimal(value) * 1000)


def gmail_message(message, account_id):
    author = parseaddr(message["headers"].get("from", ""))[1].lower()
    return Message(
        source="gmail",
        account_id=account_id,
        workspace_id="",
        channel_id="mailbox",
        thread_id=message["thread_id"],
        message_id=message["id"],
        author_id=author,
        timestamp_ms=message["internal_date"],
        text=message["body_text"],
        is_self="SENT" in message["labels"] or author == account_id.lower(),
    )
