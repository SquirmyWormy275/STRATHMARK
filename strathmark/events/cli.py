"""Thin installed operator CLI over managed-event application operations."""

import argparse
import json
import sys
from pathlib import Path

from .calculation import calculate_request
from .contracts import EventError, Principal, capabilities, encode
from .examples import synthetic_event
from .operations import EventOperations


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="STRATHMARK event operations (synthetic managed v1; local OS-private authority)."
    )
    parser.add_argument(
        "command",
        choices=(
            "capabilities",
            "example",
            "calculate",
            "event",
            "workspace",
            "workspace-account",
            "workspace-key",
        ),
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="status",
        help="create/status/forecast/fields/calculate/review/issue/outcomes/accept/advance/finalize/correct/handover/export/backup",
    )
    parser.add_argument("--database", type=Path, help="Explicit persistent operational database")
    parser.add_argument(
        "--input", type=Path, help="Operator-supplied JSON configuration or command payload"
    )
    parser.add_argument("--event-id")
    parser.add_argument(
        "--show",
        default="synthetic:missoula",
        help="Authorized show scope; local database owner is the trusted principal",
    )
    parser.add_argument("--actor", default="local-operator")
    parser.add_argument("--editor", default="cli")
    parser.add_argument(
        "--request-id", help="Stable command identity, retain across ambiguous responses"
    )
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--destination-database",
        type=Path,
        help="Pristine target for an explicit fenced local authority transfer",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable complete response")
    parser.add_argument("--auth-file", type=Path, help="Private workspace account registry")
    parser.add_argument(
        "--session-key-file", type=Path, help="Private stable Flask session key file"
    )
    parser.add_argument("--port", type=int, default=8789)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.input.read_text()) if args.input else {}
        if args.command == "capabilities":
            result = capabilities()
        elif args.command == "example":
            result = synthetic_event(args.show)
        elif args.command == "calculate":
            result = calculate_request(payload)
        elif args.command in {"workspace-account", "workspace-key"}:
            from .workspace import add_account, new_session_key

            if args.command == "workspace-account":
                if not args.auth_file:
                    parser.error("--auth-file is required")
                add_account(args.auth_file, args.actor, args.show)
            else:
                if not args.session_key_file:
                    parser.error("--session-key-file is required")
                new_session_key(args.session_key_file)
            print("Private workspace configuration saved.")
            return
        else:
            if args.database is None:
                parser.error("--database is required; no default operator storage is opened")
            principal = Principal(args.actor, frozenset({args.show}), "director")
            ops = EventOperations(args.database)
            if args.command == "workspace":
                if not args.auth_file or not args.session_key_file:
                    parser.error(
                        "workspace requires --auth-file and --session-key-file; see managed-event runbook"
                    )
                from .workspace import create_workspace

                app = create_workspace(ops, args.auth_file, args.session_key_file)
                app.run(host="127.0.0.1", port=args.port, debug=False)
                return
            if args.action == "create":
                if not args.request_id:
                    parser.error("--request-id is required")
                result = ops.create(principal, args.request_id, payload, args.editor)
            elif args.action == "transfer":
                if (
                    not args.event_id
                    or not args.request_id
                    or args.expected_revision is None
                    or not args.destination_database
                ):
                    parser.error(
                        "transfer requires event ID, request ID, expected revision and destination database"
                    )
                result = ops.transfer_to(
                    principal,
                    args.event_id,
                    args.destination_database,
                    request_id=args.request_id,
                    expected_revision=args.expected_revision,
                    editor=args.editor,
                )
            elif args.action in {"status", "open", "export", "backup"}:
                if not args.event_id:
                    parser.error("--event-id is required")
                if args.action == "export":
                    result = ops.export(principal, args.event_id)
                elif args.action == "backup":
                    if args.output is None:
                        parser.error("backup requires a new --output path")
                    result = {"backup_digest": ops.backup(principal, args.event_id, args.output)}
                    args.output = None
                else:
                    result = ops.read(principal, args.event_id)
            else:
                if not args.event_id or not args.request_id or args.expected_revision is None:
                    parser.error(
                        "mutations require --event-id, --request-id and --expected-revision"
                    )
                result = ops.execute(
                    principal,
                    args.event_id,
                    args.request_id,
                    args.expected_revision,
                    args.editor,
                    args.action,
                    payload,
                )
        if args.output:
            with args.output.open("x") as output:
                output.write(encode(result) + "\n")
        if (
            args.json
            or args.command in {"example", "calculate", "capabilities"}
            or args.action == "export"
        ):
            print(json.dumps(result, indent=2))
        else:
            print(
                f"{result.get('event_id', 'STRATHMARK')} | revision {result.get('revision', '-')} | {result.get('status', 'saved')}"
            )
            print(
                f"Editor: {result.get('editor', '-')} | Synthetic managed v1; official use disabled"
            )
            for round_ in result.get("rounds", []):
                print(f"{round_['round_id']}: {round_['state']}")
                for heat in round_["fields"]:
                    print(
                        f"  {heat['heat_id']}: {heat['state']} | stands {', '.join(heat['stands'])}"
                    )
                    for row in sorted(
                        heat.get("sheet", {}).get("rows", []), key=lambda r: r["start_mark_seconds"]
                    ):
                        print(
                            f"    {row['entry_id']}: start {row['start_mark_seconds']} s; predicted raw {row['predicted_raw_seconds']} s"
                        )
    except (EventError, OSError, ValueError, KeyError) as exc:
        print(f"STRATHMARK: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
