"""Local Gmail reply worker for the desktop application and development CLI."""

import argparse
import json
import logging
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import NamedTemporaryFile

from gmail_agent import runtime
from gmail_agent.locking import AlreadyRunning, worker_lock
from gmail_agent.store import Store


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Must be greater than zero")
    return number


def worker_options(command, *, scheduled=False):
    paths = runtime.default_paths()
    command.add_argument("--model", type=Path, default=paths["model"])
    command.add_argument("--adapter-path", type=Path)
    command.add_argument(
        "--adapter-name", help="Training persona; defaults to adapter directory name"
    )
    command.add_argument("--arm", choices=("base", "instruct"), default="instruct")
    command.add_argument("--max-tokens", type=positive, default=300)
    command.add_argument("--draft-limit", type=positive, default=20)
    command.add_argument("--model-lock", type=Path, default=paths["model_lock"])
    if scheduled:
        command.add_argument(
            "--interval", type=positive, default=1800, help="Seconds between cycles"
        )


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    try:
        paths = runtime.default_paths()
    except ValueError as error:
        result.error(str(error))
    result.add_argument("--db", type=Path, default=paths["db"])
    result.add_argument("--account", help="Connected Gmail address; optional with one account")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("info", help="Show the app interface version and default paths")
    check = commands.add_parser("self-check", help="Check the runtime without contacting Gmail")
    worker_options(check)
    check.set_defaults(model=None, max_tokens=32)
    connect = commands.add_parser(
        "connect", help="Authorize Gmail and save credentials in Keychain"
    )
    connect.add_argument("--client-secrets", type=Path, required=True)
    commands.add_parser("disconnect", help="Revoke credentials and keep local mail history")
    backfill = commands.add_parser("backfill", help="Import history without generating drafts")
    backfill.add_argument(
        "--query", default=None, help="Optional Gmail search for historical import"
    )
    backfill.add_argument("--max-pages", type=positive, help="Checkpoint after this many pages")
    backfill.add_argument(
        "--reset", action="store_true", help="Rebuild history, preserving draft records"
    )
    for name in ("once", "run"):
        command = commands.add_parser(
            name,
            help="Sync and draft once" if name == "once" else "Sync and draft every 30 minutes",
        )
        worker_options(command, scheduled=name == "run")
    service = commands.add_parser("service", help="Manage the macOS login service")
    service_commands = service.add_subparsers(dest="service_command", required=True)
    worker_options(service_commands.add_parser("install"), scheduled=True)
    for action in ("start", "stop", "status", "uninstall"):
        service_commands.add_parser(action)
    commands.add_parser(
        "status", help="Show sync progress, counts and errors without message bodies"
    )
    export = commands.add_parser(
        "export-feedback", help="Write unmasked generation/sent pairs locally"
    )
    export.add_argument("--output", type=Path, required=True)
    recover = commands.add_parser(
        "recover", help="Resolve an uncertain or failed local draft record"
    )
    recover.add_argument("draft_id", type=int)
    actions = recover.add_mutually_exclusive_group(required=True)
    actions.add_argument(
        "--adopt", metavar="GMAIL_DRAFT_ID", help="Adopt an existing draft in that thread"
    )
    actions.add_argument(
        "--dismiss", action="store_true", help="Close the local record; Gmail is unchanged"
    )
    actions.add_argument(
        "--retry", action="store_true", help="Explicitly allow another publish attempt"
    )
    return result


def select_account(store, requested):
    if requested:
        return store.account(requested.lower())["account_id"]
    accounts = store.accounts()
    if len(accounts) != 1:
        raise ValueError("Use --account to select an account, or connect one first")
    return accounts[0]["account_id"]


def recover(store, client, account_id, args):
    from gmail_agent.replies import _refresh, reconcile

    row = store.draft(args.draft_id)
    if row is None or row["account_id"] != account_id:
        raise ValueError("No such draft for this account")
    if row["state"] not in {"publishing", "published", "failed"}:
        raise ValueError("Only uncertain, published or failed records need recovery")
    _refresh(store, client, account_id, row["thread_id"], refresh_drafts=True)
    with store.db:
        reconcile(store, account_id)
    row = store.draft(args.draft_id)
    if row["state"] == "sent":
        return {"id": row["id"], "state": "sent"}
    with store.db:
        if args.adopt:
            message = client.get_draft(args.adopt)
            if message["thread_id"] != row["thread_id"]:
                raise ValueError("The Gmail draft belongs to another thread")
            if not row["generated_text"] or row["published_at"] is None:
                raise ValueError("This generation has never been published")
            store.set_state(row["id"], "published", gmail_draft_id=args.adopt, last_error=None)
        elif args.dismiss:
            store.set_state(row["id"], "dismissed", last_error=None)
        else:
            if store.draft_ids(account_id, row["thread_id"]):
                raise ValueError(
                    "A Gmail draft already exists in this thread; adopt or dismiss instead"
                )
            store.set_state(
                row["id"],
                "ready" if row["generated_text"] else "queued",
                attempts=0,
                gmail_draft_id=None,
                published_at=None,
                last_error=None,
            )
    return {"id": row["id"], "state": store.draft(row["id"])["state"]}


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = None
    client = None
    try:
        if args.command == "info":
            print(json.dumps(runtime.info(), indent=2))
            return 0
        if args.command == "self-check":
            try:
                with redirect_stdout(sys.stderr):
                    result = runtime.self_check(args)
            except Exception as error:  # noqa: BLE001 -- report synthetic diagnostic failures
                # This diagnostic uses synthetic input, never mailbox data or tokens.
                import traceback

                print("Runtime self-check failed:", file=sys.stderr)
                traceback.print_exception(error, file=sys.stderr)
                return 1
            print(json.dumps(result))
            return 0
        if args.command == "service" and args.service_command != "install":
            from gmail_agent import service

            result = getattr(service, args.service_command)(args.account)
            print(json.dumps(result, indent=2))
            return 0
        store = Store(args.db)
        if args.command == "service":
            from gmail_agent import service

            print(json.dumps(service.install(args, select_account(store, args.account)), indent=2))
            return 0
        if args.command == "status" and not args.account:
            print(json.dumps([store.status(a["account_id"]) for a in store.accounts()], indent=2))
            return 0
        if args.command == "connect":
            from gmail_agent.auth import connect

            # Third-party OAuth instructions must not corrupt the app's JSON stdout.
            with redirect_stdout(sys.stderr):
                email = connect(
                    args.client_secrets, db_path=store.path, expected_account=args.account
                )
            with store.db:
                store.add_account(email, email)
            print(json.dumps({"account": email, "connected": True}))
            return 0
        account_id = select_account(store, args.account)
        if args.command == "status":
            print(json.dumps(store.status(account_id), indent=2))
            return 0
        if args.command == "export-feedback":
            args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with NamedTemporaryFile(
                "w", encoding="utf-8", dir=args.output.parent, prefix=".feedback-"
            ) as target:
                for item in store.feedback(account_id):
                    target.write(json.dumps(item, ensure_ascii=False) + "\n")
                target.flush()
                # A hard link publishes the completed file atomically without
                # overwriting an existing output, even if another writer raced us.
                os.link(target.name, args.output)
            print(json.dumps({"output": str(args.output)}))
            return 0
        if args.command == "disconnect":
            from gmail_agent.auth import disconnect

            with worker_lock(store.path, account_id):
                disconnect(store.account(account_id)["credential_ref"])
            print(json.dumps({"disconnected": True, "local_history_retained": True}))
            return 0
        from gmail_agent.auth import client_for

        client = client_for(store.account(account_id)["credential_ref"])
        if args.command in {"backfill", "recover"}:
            with worker_lock(store.path, account_id):
                if args.command == "backfill":
                    from gmail_agent.sync import backfill

                    complete = backfill(
                        store,
                        client,
                        account_id,
                        query=args.query,
                        max_pages=args.max_pages,
                        reset=args.reset,
                    )
                    print(json.dumps({"backfill_complete": complete}))
                else:
                    print(json.dumps(recover(store, client, account_id, args)))
            return 0
        from gmail_agent.replies import LocalGenerator
        from gmail_agent.worker import run_locked

        generator = LocalGenerator(
            args.model,
            adapter_path=args.adapter_path,
            adapter_name=args.adapter_name,
            arm=args.arm,
            max_tokens=args.max_tokens,
            model_lock=args.model_lock,
        )
        # Model libraries can print memory warnings even with generation verbosity off.
        with redirect_stdout(sys.stderr):
            result = run_locked(
                store,
                client,
                account_id,
                generator,
                once=args.command == "once",
                interval=getattr(args, "interval", 1800),
                limit=args.draft_limit,
            )
        if result is not None:
            print(json.dumps(result))
        return 0
    except (ValueError, AlreadyRunning, FileExistsError, FileNotFoundError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 -- redact provider exceptions at the CLI boundary
        print(
            f"Error: {type(error).__name__}. Check status and reconnect if needed.", file=sys.stderr
        )
        return 1
    finally:
        if client is not None:
            client.session.close()
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
