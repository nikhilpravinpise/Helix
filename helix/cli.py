"""Helix CLI entry point.

Subcommands are added per subphase (see phase1-remaining-plan.md).
Implemented so far: `serve` (uvicorn over helix/api.py), `intake` (read a
signal JSON file or stdin, call the same state.intake.intake_signal the
API uses - no server needed, D6; `--run` then drives the workflow
in-process), `incidents` (list incidents), and `run` (drive an incident's
workflow graph to its next stopping point). The rest of the CLI surface
is stubbed so `helix <cmd>` parses and reports the subphase it lands in:
approve/reject/approvals (1.8), provenance (1.9).
"""

import argparse
import json
import sys

# Subcommands that exist only as stubs for later subphases: name -> where
# the real implementation lands.
_STUB_COMMANDS = {
    "approve": "1.8",
    "reject": "1.8",
    "approvals": "1.8",
    "provenance": "1.9",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helix",
        description="Helix - evidence-constrained DevOps recovery",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", help="run the Helix API server (uvicorn)")

    intake = subparsers.add_parser(
        "intake",
        help="submit a failure signal JSON; prints the incident id it created or attached to",
    )
    intake.add_argument(
        "--file",
        metavar="PATH",
        default="-",
        help="signal JSON file; omit or pass '-' to read stdin",
    )
    intake.add_argument(
        "--run",
        action="store_true",
        help="after recording the signal, drive the incident's workflow in-process",
    )

    subparsers.add_parser("incidents", help="list incidents, newest first")

    run = subparsers.add_parser(
        "run",
        help="drive an incident's workflow to its next stopping point "
        "(1.5: diagnosis -> remediation_pending)",
    )
    run.add_argument("incident_id", metavar="INC_ID", help="the incident id (INC-*)")

    for name, subphase in _STUB_COMMANDS.items():
        stub = subparsers.add_parser(name, help=f"(lands in subphase {subphase})")
        stub.add_argument("args", nargs="*", help=argparse.SUPPRESS)

    return parser


def _cmd_serve() -> int:
    import uvicorn

    from helix.config import get_settings

    settings = get_settings()
    uvicorn.run("helix.api:app", host=settings.api_bind, port=settings.api_port)
    return 0


def _read_signal_payload(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _cmd_intake(args: argparse.Namespace) -> int:
    from pydantic import ValidationError
    from sqlalchemy.exc import SQLAlchemyError

    from state.db import session_scope
    from state.intake import intake_signal

    try:
        payload = json.loads(_read_signal_payload(args.file))
    except OSError as exc:
        print(f"helix intake: cannot read signal file: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"helix intake: signal is not valid JSON: {exc}", file=sys.stderr)
        return 2

    try:
        with session_scope() as session:
            result = intake_signal(session, payload)
    except ValidationError as exc:
        print(f"helix intake: invalid signal: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(
            f"helix intake: database error (is Postgres up? see scripts/db-up.sh): {exc}",
            file=sys.stderr,
        )
        return 1

    # stdout carries just the incident id so the benchmark harness can
    # capture it directly; the created/attached detail goes to stderr.
    print(result.incident_id)
    verb = "created" if result.created else "attached to"
    print(f"helix intake: {verb} incident {result.incident_id}", file=sys.stderr)

    if args.run:
        _run_hook(result.incident_id)
    return 0


def _run_hook(incident_id: str) -> None:
    """The intake -> run hook (plan.md 1.4's in-process direct call, D6):
    drive the workflow graph after a signal is recorded. The signal is
    already persisted, so a run failure is reported but never fails the
    intake itself."""
    from agent.runner import run_incident

    try:
        result = run_incident(incident_id)
    except Exception as exc:
        print(f"helix intake --run: run failed: {exc}", file=sys.stderr)
        return
    print(
        f"helix intake --run: status={result.status} run={result.run_status}",
        file=sys.stderr,
    )
    if result.error:
        print(f"helix intake --run: run error: {result.error}", file=sys.stderr)


def _cmd_run(args: argparse.Namespace) -> int:
    from agent.runner import IncidentNotFoundError, RunConflictError, run_incident

    try:
        result = run_incident(args.incident_id)
    except IncidentNotFoundError:
        print(f"helix run: no such incident: {args.incident_id}", file=sys.stderr)
        return 2
    except RunConflictError as exc:
        print(f"helix run: {exc}", file=sys.stderr)
        return 2

    print(f"{result.incident_id}: status={result.status} run={result.run_status}")
    if result.hypothesis:
        hyp = result.hypothesis
        print(f"hypothesis: {hyp['claim']} (confidence={hyp['confidence']})")
        print(f"evidence_refs: {', '.join(hyp['evidence_refs']) or '(none)'}")
    for note in result.errors:
        print(f"note: {note}", file=sys.stderr)
    if result.error:
        print(f"helix run: run failed: {result.error}", file=sys.stderr)
        return 1
    return 0


def _cmd_incidents() -> int:
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    from state import models
    from state.db import session_scope

    try:
        with session_scope() as session:
            rows = session.scalars(
                select(models.Incident).order_by(
                    models.Incident.created_at.desc(), models.Incident.id.desc()
                )
            ).all()
            for incident in rows:
                print(
                    f"{incident.incident_id}  {incident.status:<19} {incident.service:<16} "
                    f"{incident.environment:<11} {incident.severity}"
                )
    except SQLAlchemyError as exc:
        print(
            f"helix incidents: database error (is Postgres up? see scripts/db-up.sh): {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "serve":
        return _cmd_serve()
    if args.command == "intake":
        return _cmd_intake(args)
    if args.command == "incidents":
        return _cmd_incidents()
    if args.command == "run":
        return _cmd_run(args)
    # Stub subcommand: parses, then reports where it lands.
    print(
        f"helix {args.command}: not implemented yet "
        f"(lands in subphase {_STUB_COMMANDS[args.command]})",
        file=sys.stderr,
    )
    return 2
