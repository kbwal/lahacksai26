"""Translate mailbox snapshots to the training branch's conversation contract."""

from email.utils import parseaddr


def conversation_messages(messages, account_email):
    return [
        {
            "from": "me"
            if "SENT" in message["labels"]
            or parseaddr(message["headers"].get("from", ""))[1].lower() == account_email.lower()
            else "them",
            "text": message["body_text"],
        }
        for message in messages[-3:]
    ]


def generation_input(context, adapter_name):
    from train import format as training_format

    # infra and train can land in either order. Remove the Pair fallback once
    # Conversation is on main; do not flatten message roles for the new trainer.
    if hasattr(training_format, "Conversation"):
        return training_format.Conversation.from_dict({**context, "adapter_name": adapter_name})
    return training_format.Pair.from_dict(context)
