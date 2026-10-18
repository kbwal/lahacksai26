"""Read selected Slack/Discord conversations into a local, source-labelled store."""

import argparse
import getpass
import json
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import NamedTemporaryFile

from gmail_agent.__main__ import positive, worker_options
from gmail_agent.auth import keychain
from gmail_agent.locking import worker_lock
from gmail_agent.runtime import default_paths
from message_pipeline import SOURCES
from message_pipeline.clients import client_for
from message_pipeline.store import Disconnected, Store, export_gmail
from message_pipeline.sync import sync_once

SERVICE = "lahacks-mail.messages"


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    paths = default_paths()
    result.add_argument("--db", type=Path, default=paths["home"] / "data" / "messages.db")
    result.add_argument("--source", choices=SOURCES, required=True)
    result.add_argument("--account", help="Slack user ID, Discord human user ID, or Gmail address")
    result.add_argument("--workspace", default="", help="Slack team ID or Discord server ID")
    commands = result.add_subparsers(dest="command", required=True)
    connect = commands.add_parser(
        "connect", help="Validate an existing app token; save in Keychain"
    )
    connect.add_argument("--channels", nargs="+", required=True)
    connect.add_argument(
        "--token-env", help="Name of an environment variable; otherwise prompt securely"
    )
    for name in ("sync", "run"):
        command = commands.add_parser(name)
        command.add_argument(
            "--max-pages", type=positive, default=100, help="Per channel per cycle"
        )
        if name == "run":
            command.add_argument("--interval", type=positive, default=1800)
        command.add_argument("--suggest-replies", action="store_true")
        worker_options(command)
    suggest = commands.add_parser(
        "suggest", help="Generate a local reply for a cached conversation"
    )
    suggest.add_argument("--channel", required=True)
    suggest.add_argument("--thread", required=True)
    worker_options(suggest)
    commands.add_parser("suggestions", help="Read local suggestions and observed human replies")
    dismiss = commands.add_parser("dismiss", help="Dismiss a local suggestion")
    dismiss.add_argument("suggestion_id", type=int)
    commands.add_parser("status")
    disconnect = commands.add_parser(
        "disconnect", help="Remove local credential, optionally history"
    )
    disconnect.add_argument("--purge", action="store_true")
    export = commands.add_parser("export", help="Export context-only normalized message JSONL")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--gmail-db", type=Path, default=paths["db"])
    return result


def write_jsonl(items, output):
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    count = 0
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=output.parent, prefix=".messages-"
    ) as target:
        for item in items:
            target.write(json.dumps(item, ensure_ascii=False) + "\n")
            count += 1
        target.flush()
        # No overwrite, including when an existing export is a symlink.
        os.link(target.name, output)
    return {"output": str(output.resolve()), "messages": count}


def connect(store, args):
    if args.source == "discord" and (
        not (args.account or "").isdigit() or not args.workspace.isdigit()
    ):
        raise ValueError(
            "Discord connect requires --account HUMAN_USER_ID and --workspace SERVER_ID"
        )
    token = os.environ.get(args.token_env) if args.token_env else getpass.getpass("App token: ")
    if not token or not token.strip():
        raise ValueError("No app token supplied")
    client = client_for(args.source, token.strip())
    try:
        if args.source == "slack":
            account, workspace = client.identity()
            if (
                args.account
                and args.account != account
                or args.workspace
                and args.workspace != workspace
            ):
                raise ValueError("Slack identity does not match the requested account/workspace")
        else:
            client.identity()
            account, workspace = args.account, args.workspace
        for channel in args.channels:
            client.validate_channel(channel, workspace)
        key = (args.source, account, workspace)
        credential_ref = json.dumps(key, separators=(",", ":"))
        with worker_lock(store.path, json.dumps(key)):
            keychain().set_password(SERVICE, credential_ref, token.strip())
            store.connect(key, credential_ref, args.channels)
        return {
            "source": args.source,
            "account_id": account,
            "workspace_id": workspace,
            "connected": True,
            "channels": args.channels,
        }
    finally:
        client.close()


def suggest_replies(store, args, key):
    from gmail_agent.replies import LocalGenerator
    from message_pipeline.replies import generate_suggestions

    generator = LocalGenerator(
        args.model,
        adapter_path=args.adapter_path,
        adapter_name=args.adapter_name,
        arm=args.arm,
        max_tokens=args.max_tokens,
        model_lock=args.model_lock,
    )
    try:
        with redirect_stdout(sys.stderr):
            return generate_suggestions(
                store,
                key,
                generator,
                channel_id=getattr(args, "channel", None),
                thread_id=getattr(args, "thread", None),
                limit=args.draft_limit,
                include_history=args.command == "suggest",
            )
    finally:
        generator.close()


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    store = client = None
    try:
        if args.source == "gmail":
            if args.command != "export" or not args.account:
                raise ValueError(
                    "Use the Gmail worker for sync/connect; messages supports Gmail export"
                )
            result = write_jsonl(export_gmail(args.gmail_db, args.account.lower()), args.output)
        else:
            store = Store(args.db)
            if args.command == "connect":
                result = connect(store, args)
            else:
                if not args.account or not args.workspace:
                    raise ValueError("Use --account and --workspace from the connect result")
                key = (args.source, args.account, args.workspace)
                connection = store.connection(key)
                if args.command == "status":
                    result = store.status(key)
                elif args.command == "suggest":
                    result = suggest_replies(store, args, key)
                    result["suggestions"] = [
                        item
                        for item in store.suggestions(key)
                        if item["channel_id"] == args.channel and item["thread_id"] == args.thread
                    ]
                elif args.command == "suggestions":
                    result = list(store.suggestions(key))
                elif args.command == "dismiss":
                    from message_pipeline.replies import dismiss

                    dismiss(store, key, args.suggestion_id)
                    result = {"source": args.source, "dismissed": args.suggestion_id}
                elif args.command == "export":
                    result = write_jsonl(store.export(key), args.output)
                elif args.command == "disconnect":
                    with worker_lock(store.path, json.dumps(key)):
                        vault = keychain()
                        if vault.get_password(SERVICE, connection["credential_ref"]):
                            vault.delete_password(SERVICE, connection["credential_ref"])
                        if args.purge:
                            with store.db:
                                store.db.execute(
                                    "DELETE FROM connections WHERE source=? AND account_id=? AND workspace_id=?",
                                    key,
                                )
                    result = {
                        "disconnected": True,
                        "local_history_retained": not args.purge,
                        "provider_token_revoked": False,
                    }
                else:
                    token = keychain().get_password(SERVICE, connection["credential_ref"])
                    if not token:
                        raise Disconnected("No credential in Keychain; connect again")
                    client = client_for(args.source, token)

                    def credential_check(current):
                        saved = keychain().get_password(SERVICE, current["credential_ref"])
                        if not saved:
                            raise Disconnected("Connection was disconnected; stop this worker")
                        client.session.headers["Authorization"] = f"{client.auth_scheme} {saved}"

                    while True:
                        try:
                            result = sync_once(
                                store,
                                client,
                                key,
                                max_pages=args.max_pages,
                                credential_check=credential_check,
                            )
                            if args.suggest_replies and not result["deferred"]:
                                result["replies"] = suggest_replies(store, args, key)
                        except Disconnected:
                            raise
                        except Exception as error:
                            if args.command != "run":
                                raise
                            result = {**store.status(key), "error": type(error).__name__}
                        if args.command != "run":
                            break
                        print(json.dumps(result), flush=True)
                        time.sleep(args.interval)
        print(json.dumps(result), flush=True)
        return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, FileExistsError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 -- redact tokens/provider bodies at the CLI boundary
        print(
            f"Error: {type(error).__name__}; check permissions and connection status",
            file=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            client.close()
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
