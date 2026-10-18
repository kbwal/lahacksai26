"""Translate mailbox snapshots to the training branch's conversation contract."""

from email.utils import parseaddr

from gmail_agent.text import clean_text


def from_me(message, account_email):
    return "SENT" in message["labels"] or (
        parseaddr(message["headers"].get("from", ""))[1].lower() == account_email.lower()
    )


def conversation_messages(messages, account_email):
    """The thread as pipeline/gmail.py exports it for training: cleaned text, automated
    and empty messages dropped, and only Gmail's SENT label marks the user's own turns.
    The drafter keeps as many of the newest messages as fit its context."""
    from gmail_agent.client import is_automated

    conversation = []
    for message in messages:
        text = clean_text(message["body_text"])
        if text and not is_automated(message):
            conversation.append({"from": "me" if "SENT" in message["labels"] else "them", "text": text})
    return conversation


def generation_input(context, adapter_name):
    from train import format as training_format

    context = {"source": "gmail", **context}
    return training_format.Conversation.from_dict({**context, "adapter_name": adapter_name})
