#!/usr/bin/env python3
"""Inspect and resolve one Mac delivery receipt after checking Codex Desktop.

Resolution never sends a prompt, creates a thread, or deletes an old request ID.
Stop Bridge first and check the original operation in Desktop. Use confirmed
when it was delivered, or refused when deliberately abandoning that attempt.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from delivery_journal import DeliveryJournal


DEFAULT_JOURNAL = (Path.home() / "Library" / "Application Support"
                   / "MobileCodexBridge" / "delivery.sqlite")
PUBLIC_FIELDS = ("owner", "request_id", "scope", "thread_id", "state", "phase", "updated")


def receipt_metadata(record):
    return {key: record[key] for key in PUBLIC_FIELDS}


def require_stopped_bridge(port: int) -> None:
    try:
        connection = socket.create_connection(("127.0.0.1", port), timeout=1)
    except ConnectionRefusedError:
        return
    except OSError as error:
        raise ValueError("Cannot verify that Bridge is stopped; no receipt was changed.") from error
    connection.close()
    raise ValueError("A service is still listening on the Bridge port. Stop Bridge first.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL,
                        help="Use delivery.sqlite beside a custom device registry, if configured.")
    actions = parser.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list", help="List pending receipt metadata without prompts or credentials.")
    listing.add_argument("--limit", type=int, default=50)
    resolve = actions.add_parser("resolve", help="Finalize exactly one receipt after a manual Desktop check.")
    resolve.add_argument("--outcome", choices=("confirmed", "refused"), required=True)
    resolve.add_argument("--thread-id", help="Required to confirm a create whose thread ID was lost.")
    abandon = actions.add_parser("abandon-unseen", help="Retire a browser request ID absent from this journal.")
    abandon.add_argument("--scope", required=True, help="create, or the original target thread ID.")
    for action in (resolve, abandon):
        action.add_argument("--owner", required=True, help="Device ID, or owner from the list output.")
        action.add_argument("--request-id", required=True)
        action.add_argument("--bridge-port", type=int, default=4317)
        action.add_argument("--confirm-stopped-and-reviewed", action="store_true",
                            help="Acknowledge that Bridge is stopped and Desktop has been checked.")
    args = parser.parse_args(argv)
    path = args.journal.expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        parser.error("The journal must be an existing regular file; no database was created.")
    try:
        if args.action == "list":
            if not 1 <= args.limit <= 500:
                parser.error("--limit must be between 1 and 500")
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT owner,request_id,scope,thread_id,state,phase,updated FROM deliveries "
                    "WHERE state IN ('claimed','uncertain') ORDER BY updated DESC LIMIT ?",
                    (args.limit,),
                ).fetchall()
            print(json.dumps([receipt_metadata(row) for row in rows], indent=2))
            return 0
        if not args.confirm_stopped_and_reviewed:
            parser.error("Stop Bridge and check Desktop, then pass --confirm-stopped-and-reviewed.")
        if not 1 <= args.bridge_port <= 65535:
            parser.error("--bridge-port must be between 1 and 65535")
        require_stopped_bridge(args.bridge_port)
        journal = DeliveryJournal(path)
        if args.action == "abandon-unseen":
            record = journal.abandon_unseen(args.owner, args.request_id, args.scope)
        else:
            record = journal.resolve_pending(
                args.owner, args.request_id, args.outcome, thread_id=args.thread_id,
            )
        print(json.dumps(receipt_metadata(record), indent=2))
        print("Receipt finalized. Its request ID cannot dispatch again. Restart Bridge to refresh the browser.")
        return 0
    except ValueError as error:
        parser.error(str(error))
    except (OSError, sqlite3.Error) as error:
        parser.error("Journal operation failed: " + type(error).__name__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
