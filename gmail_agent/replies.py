"""Select reply turns, publish native drafts, and map human replies by thread."""

import gc
import hashlib
import json
import re
from contextlib import ExitStack
from email.utils import parseaddr
from pathlib import Path

from gmail_agent.client import (
    GmailError,
    PublishUncertain,
    is_automated,
    reply_raw,
    reply_recipient,
)
from gmail_agent.locking import AlreadyRunning, process_lock
from gmail_agent.runtime import default_paths
from gmail_agent.store import now_ms
from gmail_agent.text import comparable_body
from gmail_agent.training import conversation_messages, generation_input


def candidate(store, account_id, thread_id):
    account = store.account(account_id)
    messages = [
        m for m in store.thread_messages(account_id, thread_id) if "DRAFT" not in m["labels"]
    ]
    if not messages or store.draft_ids(account_id, thread_id):
        return None
    latest = messages[-1]
    labels = set(latest["labels"])
    if (
        "INBOX" not in labels
        or labels & {"SENT", "SPAM", "TRASH"}
        or latest["internal_date"] < account["activated_at"]
        or parseaddr(latest["headers"].get("from", ""))[1].lower() == account["email"].lower()
        or is_automated(latest)
        or not reply_recipient(latest)
        or not latest["headers"].get("message-id")
    ):
        return None
    if any("DRAFT" in m["labels"] for m in store.thread_messages(account_id, thread_id)):
        return None
    return latest, messages


def context_for(trigger, messages, account_email):
    # Include the incoming message being answered. Preserve this exact context for feedback.
    return {
        "source": "gmail",
        "recipient": reply_recipient(trigger),
        "message_ids": [m["id"] for m in messages],
        "conversation": conversation_messages(messages, account_email),
    }


def queue_candidates(store, account_id, thread_ids):
    active = {row["thread_id"] for row in store.active_drafts(account_id)}
    queued = 0
    for thread_id in sorted(thread_ids - active):
        result = candidate(store, account_id, thread_id)
        if result:
            trigger, messages = result
            queued += store.enqueue(
                account_id, thread_id, trigger["id"], context_for(trigger, messages, account_id)
            )
    return queued


def reconcile(store, account_id):
    closed = 0
    for draft in store.active_drafts(account_id):
        if draft["state"] not in {"publishing", "published"}:
            continue
        messages = store.thread_messages(account_id, draft["thread_id"])
        sent = [
            m
            for m in messages
            if "SENT" in m["labels"] and m["internal_date"] >= draft["published_at"]
        ]
        if sent:
            indexed_draft = draft["gmail_draft_id"] in store.draft_ids(
                account_id, draft["thread_id"]
            )
            surviving_draft = any("DRAFT" in m["labels"] for m in messages)
            if indexed_draft and not surviving_draft:
                # A send can race the cycle's draft listing. Wait for the next
                # index rather than misclassifying the newer thread snapshot.
                continue
            if draft["state"] == "publishing" or indexed_draft:
                # A separate reply is not evidence that this suggestion was used.
                # Preserve the remote draft: it may contain the person's edits.
                store.set_state(
                    draft["id"],
                    "superseded",
                    last_error="Separate reply observed; inspect any remaining Gmail draft",
                )
            else:
                store.set_state(draft["id"], "sent", sent_message_id=sent[0]["id"], last_error=None)
            closed += 1
        elif (
            draft["state"] == "published"
            and draft["gmail_draft_id"] not in store.draft_ids(account_id, draft["thread_id"])
            and not any("DRAFT" in m["labels"] for m in messages)
        ):
            store.set_state(draft["id"], "dismissed", last_error=None)
            closed += 1
    return closed


def _refresh(store, client, account_id, thread_id, *, refresh_drafts=False):
    drafts = client.list_drafts() if refresh_drafts else None
    messages = client.get_thread(thread_id)
    with store.db:
        store.replace_thread(account_id, thread_id, messages)
        if drafts is not None:
            store.set_draft_index(account_id, drafts)


def recover_publishing(store, client, account_id):
    """Adopt an exact surviving draft after a lost create response; otherwise leave blocked."""
    for row in store.active_drafts(account_id):
        if row["state"] != "publishing":
            continue
        matches = []
        for draft_id in store.draft_ids(account_id, row["thread_id"]):
            try:
                message = client.get_draft(draft_id)
            except GmailError as error:
                if error.status == 404:
                    continue  # The person may have sent or discarded it since listing.
                raise
            if message["thread_id"] == row["thread_id"] and comparable_body(
                message["body_text"]
            ) == comparable_body(row["generated_text"]):
                matches.append(draft_id)
        with store.db:
            if len(matches) == 1:
                store.set_state(row["id"], "published", gmail_draft_id=matches[0], last_error=None)
            else:
                store.set_state(
                    row["id"],
                    "publishing",
                    last_error="Unknown publication outcome; inspect Gmail and use recover",
                )


def _preflight(store, client, account_id, draft_id):
    row = store.draft(draft_id)
    _refresh(store, client, account_id, row["thread_id"])
    current = candidate(store, account_id, row["thread_id"])
    with store.db:
        if current is None or current[0]["id"] != row["trigger_message_id"]:
            store.set_state(draft_id, "superseded", last_error=None)
            queue_candidates(store, account_id, {row["thread_id"]})
            return None
        trigger, messages = current
        context = context_for(trigger, messages, account_id)
        saved = json.loads(row["context"])
        if "adapter_name" in saved:
            context["adapter_name"] = saved["adapter_name"]
        if context != saved:
            store.set_state(
                draft_id,
                "queued",
                context=json.dumps(context),
                generated_text=None,
                model_version=None,
                last_error=None,
            )
            return None
    return trigger


def process_pending(store, client, account_id, generator, *, limit=20):
    recover_publishing(store, client, account_id)
    rows = store.db.execute(
        """SELECT id FROM reply_drafts WHERE account_id=? AND state IN ('queued','ready')
           ORDER BY id LIMIT ?""",
        (account_id, limit),
    ).fetchall()
    published = 0
    model_busy = False
    for item in rows:
        draft_id = item["id"]
        try:
            if _preflight(store, client, account_id, draft_id) is None:
                continue
            row = store.draft(draft_id)
            if row["state"] == "queued":
                # Record attempts before model work, which can fail independently of sync.
                with store.db:
                    store.set_state(draft_id, "queued", attempts=row["attempts"] + 1)
                context = {**json.loads(row["context"]), "adapter_name": generator.adapter_name}
                text = generator.generate(context)
                if not text.strip():
                    raise ValueError("Model returned an empty draft")
                with store.db:
                    store.set_state(
                        draft_id,
                        "ready",
                        generated_text=text,
                        context=json.dumps(context),
                        model_version=generator.version,
                        last_error=None,
                    )
            trigger = _preflight(store, client, account_id, draft_id)
            if trigger is None:
                continue
            row = store.draft(draft_id)
            raw = reply_raw(store.account(account_id)["email"], trigger, row["generated_text"])
            with store.db:
                store.set_state(draft_id, "publishing", published_at=now_ms(), last_error=None)
            result = client.create_draft(row["thread_id"], raw)
            with store.db:
                store.set_state(draft_id, "published", gmail_draft_id=result["id"], last_error=None)
            published += 1
        except AlreadyRunning:
            # Training or another account can own the model temporarily. This is
            # not a failed generation and must not consume the retry budget.
            row = store.draft(draft_id)
            with store.db:
                store.set_state(
                    draft_id, "queued", attempts=row["attempts"] - 1, last_error="Model is busy"
                )
            model_busy = True
            break
        except PublishUncertain:
            with store.db:
                store.set_state(draft_id, "publishing", last_error="Unknown publication outcome")
        except Exception as error:  # noqa: BLE001 -- isolate each draft and preserve uncertain writes
            row = store.draft(draft_id)
            # Never downgrade an uncertain write into a retryable state.
            state = row["state"]
            if state == "publishing" and isinstance(error, GmailError):
                state = "failed"  # An explicit rejecting HTTP response is safe to retry manually.
            elif state in {"queued", "ready"} and row["attempts"] >= 3:
                state = "failed"
            with store.db:
                store.set_state(draft_id, state, last_error=type(error).__name__)
    return {"published": published, "model_busy": model_busy}


def adapter_config(adapter_path):
    path = Path(adapter_path) / "adapter_config.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def resolve_arm(model_path, config, requested=None):
    """The adapter's training arm wins; without one, the model name decides."""
    trained = config.get("arm")
    if trained and requested and trained != requested:
        raise ValueError(f"The adapter was trained with --arm {trained}, not {requested}")
    if trained or requested:
        return trained or requested
    return "base" if Path(model_path).name.lower().endswith("-base") else "instruct"


def check_base_model(model_path, config):
    # Training records the model path it started from; compare checkpoints by name.
    trained = config.get("model")
    if trained and Path(trained).name != Path(model_path).name:
        raise ValueError(
            f"The adapter was trained on {Path(trained).name}, not {Path(model_path).name}"
        )


class LocalGenerator:
    def __init__(
        self,
        model_path,
        *,
        adapter_path=None,
        adapter_root=None,
        adapter_name=None,
        arm=None,
        max_tokens=300,
        model_lock=None,
    ):
        self.model_path = Path(model_path).resolve()
        self.adapter_name = (
            adapter_name if adapter_name is not None
            else Path(adapter_path).name if adapter_path else "professional"
        )
        if not re.fullmatch(r"[\w-]+", self.adapter_name):
            raise ValueError("Use --adapter-name with letters, digits, _ or -")
        self._explicit_adapter_path = Path(adapter_path).resolve() if adapter_path else None
        self.adapter_root = Path(adapter_root).resolve() if adapter_root is not None else None
        self._requested_arm = arm
        self._resolve_adapter()
        self.max_tokens = max_tokens
        self.model_lock = model_lock if model_lock is not None else default_paths()["model_lock"]
        self.model = None
        self.tokenizer = None
        self.stack = ExitStack()
        self.version = "unloaded"

    def _resolve_adapter(self):
        self.adapter_path = self._explicit_adapter_path
        if self.adapter_path is None and self.adapter_root is not None:
            candidate = self.adapter_root / self.adapter_name
            if (candidate / "adapters.safetensors").is_file():
                self.adapter_path = candidate
        config = adapter_config(self.adapter_path) if self.adapter_path else {}
        check_base_model(self.model_path, config)
        self.arm = resolve_arm(self.model_path, config, self._requested_arm)

    def generate(self, context):
        if self.model is None:
            if not (self.model_path / "config.json").is_file():
                raise ValueError("Local model path is missing config.json")
            self.stack.enter_context(process_lock(self.model_lock))
            try:
                # A daemon survives multiple cycles and adapter installs. Resolve and
                # validate the files again while the installer cannot replace them.
                self._resolve_adapter()
                from mlx_lm import load

                self.model, self.tokenizer = load(
                    str(self.model_path),
                    adapter_path=str(self.adapter_path) if self.adapter_path else None,
                )
                # Identify the local checkpoint and adapter loaded by this process.
                signature = [
                    (str(p), p.stat().st_size, p.stat().st_mtime_ns)
                    for root in (self.model_path, self.adapter_path)
                    if root
                    for p in sorted(root.glob("*.safetensors"))
                ]
                digest = hashlib.sha256(json.dumps(signature).encode()).hexdigest()[:16]
                self.version = f"{self.model_path.name}:{self.arm}:{digest}"
            except BaseException:
                self.close()
                raise
        from train.draft import draft

        conversation = generation_input(context, self.adapter_name)
        return draft(self.model, self.tokenizer, conversation, self.arm, self.max_tokens)

    def close(self):
        loaded = self.model is not None
        self.model = self.tokenizer = None
        try:
            if loaded:
                import mlx.core as mx

                gc.collect()
                mx.clear_cache()
        finally:
            self.stack.close()
