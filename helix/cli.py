"""Helix CLI entry point.

Subcommands are added per subphase (see phase1-remaining-plan.md).
Implemented so far: `serve` (uvicorn over helix/api.py), `intake` (read a
signal JSON file or stdin, call the same state.intake.intake_signal the
API uses - no server needed, D6; `--run` then drives the workflow
in-process), `incidents` (list incidents), `run` (drive an incident's
workflow graph to its next stopping point), the 1.8 approval surface
(`approvals` lists the gate's pending/decided rows; `approve`/`reject`
record the decision on an incident's pending approvals row and resume
the suspended run), and `provenance` (1.9 - print an incident's decision
provenance as Markdown).
"""

import argparse
import json
import sys


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
        "(diagnosis -> plan -> validate -> execute -> verify, ending "
        "recovered/escalated/failed or suspending at awaiting_approval "
        "when policy requires a decision)",
    )
    run.add_argument("incident_id", metavar="INC_ID", help="the incident id (INC-*)")

    approvals = subparsers.add_parser(
        "approvals",
        help="list approval requests (pending by default; --all includes decided)",
    )
    approvals.add_argument(
        "--all", action="store_true", help="include decided rows, not just pending"
    )
    approvals.add_argument("--incident", metavar="INC_ID", default=None, help="only this incident")

    approve = subparsers.add_parser(
        "approve",
        help="approve an incident's pending action and resume its suspended run",
    )
    approve.add_argument("incident_id", metavar="INC_ID", help="the incident id (INC-*)")
    approve.add_argument(
        "--actor",
        default=None,
        help="approver identity recorded on the row (default: HELIX_OPERATOR)",
    )
    approve.add_argument("--note", default="", help="optional decision note")

    reject = subparsers.add_parser(
        "reject",
        help="reject an incident's pending action; the incident replans without it",
    )
    reject.add_argument("incident_id", metavar="INC_ID", help="the incident id (INC-*)")
    reject.add_argument(
        "--actor",
        default=None,
        help="decider identity recorded on the row (default: HELIX_OPERATOR)",
    )
    reject.add_argument("--note", default="", help="optional decision note")

    provenance = subparsers.add_parser(
        "provenance",
        help="print an incident's decision provenance as Markdown "
        "(trigger -> evidence -> hypothesis -> candidates -> decision -> "
        "validation -> approval -> execution -> verification)",
    )
    provenance.add_argument("incident_id", metavar="INC_ID", help="the incident id (INC-*)")

    consume = subparsers.add_parser(
        "consume",
        help="consume canonical events from Kafka and drive incident state "
        "(subphase E4); correlates across sources and is safe to re-run, "
        "because a replayed event is recorded once and executes nothing",
    )
    consume.add_argument(
        "--group",
        default="helix-ingest",
        help="consumer group id (default: helix-ingest). A new group reads "
        "from the beginning with --from-beginning; an existing one always "
        "resumes from its committed offsets",
    )
    consume.add_argument(
        "--from-beginning",
        action="store_true",
        help="for a new group, start at the oldest retained event instead of "
        "only what arrives from now on",
    )
    consume.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="stop after handling this many events (default: run until the topic goes quiet)",
    )

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
    if result.selected_action:
        act = result.selected_action
        print(f"selected: {act['action']} {act['params']} (score={act['score']})")
    if result.execution:
        exe = result.execution
        print(
            f"execution: {exe['action']} -> {exe['result']} ({exe['detail']}) "
            f"[policy={exe['policy_decision']}, origin={exe['origin']}]"
        )
    if result.verification:
        ver = result.verification
        print(f"verification: {ver['action']} -> {ver['outcome']} ({ver['detail']})")
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


def _cmd_approvals(args: argparse.Namespace) -> int:
    """List approval rows - pending by default, `--all` includes decided."""
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    from state import models
    from state.db import session_scope

    try:
        with session_scope() as session:
            query = select(models.Approval).order_by(models.Approval.id.desc())
            if not args.all:
                query = query.where(models.Approval.status == models.ApprovalStatus.PENDING)
            if args.incident:
                query = query.join(models.Incident).where(
                    models.Incident.incident_id == args.incident
                )
            # Extract plain values inside the session - the rows go
            # detached as soon as the scope exits.
            rows = [
                (
                    row.id,
                    row.incident.incident_id,
                    row.action,
                    row.status.value,
                    row.decided_by,
                    row.decided_at,
                    row.params,
                )
                for row in session.scalars(query).all()
            ]
    except SQLAlchemyError as exc:
        print(
            f"helix approvals: database error (is Postgres up? see scripts/db-up.sh): {exc}",
            file=sys.stderr,
        )
        return 1
    if not rows:
        print("no approvals pending" if not args.all else "no approvals")
        return 0
    for row_id, incident_id, action, status, decided_by, decided_at, params in rows:
        decided = f" by {decided_by} at {decided_at}" if decided_by else ""
        print(f"#{row_id}  {incident_id}  {action:<20} {status:<8}{decided}  params={params}")
    return 0


def _cmd_decide(args: argparse.Namespace, approved: bool) -> int:
    """Record the approve/reject decision on the incident's pending
    approvals row, then resume the suspended run.

    Idempotent: an already-decided row reports its recorded decision and
    returns without resuming - a second approve can never double-execute.
    The decision commits before the resume so the runner's sessions see it;
    a resume conflict (another process already driving) is reported, not
    fatal - the decision itself is durable."""
    verb = "approve" if approved else "reject"
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    from helix.config import get_settings
    from state import models
    from state.approvals import latest_for_incident, pending_for_incident, record_decision
    from state.db import session_scope

    actor = args.actor or get_settings().operator
    from state.approvals import ApprovalDecidedError

    try:
        with session_scope() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == args.incident_id)
            )
            if incident is None:
                print(
                    f"helix {verb}: no such incident: {args.incident_id}",
                    file=sys.stderr,
                )
                return 2
            pending = pending_for_incident(session, incident.id)
            if pending is None:
                latest = latest_for_incident(session, incident.id)
                if latest is not None:
                    print(
                        f"{args.incident_id}: approval #{latest.id} already "
                        f"{latest.status.value} by {latest.decided_by} - nothing to decide",
                    )
                    return 0
                print(
                    f"helix {verb}: no pending approval for {args.incident_id}",
                    file=sys.stderr,
                )
                return 2
            record_decision(session, pending, approved, actor, args.note)
            approval_id = pending.id
            action = pending.action
    except ApprovalDecidedError as exc:
        # A race decided first - or the pending row outlived
        # HELIX_APPROVAL_TIMEOUT (1.10): a clean refusal, never a crash.
        print(f"helix {verb}: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(
            f"helix {verb}: database error (is Postgres up? see scripts/db-up.sh): {exc}",
            file=sys.stderr,
        )
        return 1

    decision = "approved" if approved else "rejected"
    print(f"{args.incident_id}: approval #{approval_id} {decision} by {actor} ({action})")

    from agent.runner import RunConflictError, run_incident

    try:
        result = run_incident(args.incident_id)
    except RunConflictError as exc:
        # Another process holds the run row; the decision is durable and
        # that run (or a later `helix run`) picks it up.
        print(f"helix {verb}: decision recorded; resume deferred: {exc}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(
            f"helix {verb}: decision recorded; resume failed "
            f"(`helix run {args.incident_id}` drives it later): {exc}",
            file=sys.stderr,
        )
        return 0
    print(f"{result.incident_id}: status={result.status} run={result.run_status}")
    if result.execution:
        exe = result.execution
        print(f"execution: {exe['action']} -> {exe['result']} ({exe['detail']})")
    if result.verification:
        ver = result.verification
        print(f"verification: {ver['action']} -> {ver['outcome']} ({ver['detail']})")
    for note in result.errors:
        print(f"note: {note}", file=sys.stderr)
    if result.error:
        print(f"helix {verb}: run failed: {result.error}", file=sys.stderr)
        return 1
    return 0


def _cmd_consume(args: argparse.Namespace) -> int:
    """Drive incident state from the event stream (subphase E4).

    Each event is handled in its own transaction, so one failure cannot
    roll back the incidents its predecessors opened, and the redelivery
    that follows is harmless because ingest is idempotent.
    """
    import functools

    from sqlalchemy import create_engine

    from events.ingest import run_consumer
    from helix.config import get_settings
    from state.db import session_scope

    engine = create_engine(get_settings().database_url)
    handled = run_consumer(
        functools.partial(session_scope, engine),
        group_id=args.group,
        max_events=args.max_events,
        from_beginning=args.from_beginning,
    )
    print(f"handled {handled} event(s)")
    return 0


def _cmd_provenance(args: argparse.Namespace) -> int:
    """Print the incident's decision-provenance record as Markdown -
    state.provenance.render_markdown over the assembled record."""
    from sqlalchemy.exc import SQLAlchemyError

    from state.db import session_scope
    from state.provenance import assemble_provenance, render_markdown

    try:
        with session_scope() as session:
            record = assemble_provenance(session, args.incident_id)
    except SQLAlchemyError as exc:
        print(
            f"helix provenance: database error (is Postgres up? see scripts/db-up.sh): {exc}",
            file=sys.stderr,
        )
        return 1
    if record is None:
        print(f"helix provenance: no such incident: {args.incident_id}", file=sys.stderr)
        return 2
    print(render_markdown(record), end="")
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
    if args.command == "approvals":
        return _cmd_approvals(args)
    if args.command == "approve":
        return _cmd_decide(args, approved=True)
    if args.command == "reject":
        return _cmd_decide(args, approved=False)
    if args.command == "consume":
        return _cmd_consume(args)
    if args.command == "provenance":
        return _cmd_provenance(args)
    return 2
