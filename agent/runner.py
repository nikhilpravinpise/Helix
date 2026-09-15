"""Run driver: `helix run INC-*` drives an incident's workflow to its next
stopping point - terminal status, suspension (D13 checkpoint), or the
1.5 end state at remediation_pending.

Run bookkeeping lives in `workflow_runs`: `start_run` refuses while
another run for the incident is `running` (the single-active-run guard;
the partial unique index in migration 0002 is the hard backstop, full
lifecycle hardening is 1.10). A run ends `terminal` when the graph
finishes, `suspended` when work is parked (interrupt or
awaiting_approval), `failed` on an unexpected error.

Resume semantics: a `suspended` run is continued by thread_id =
incident_id, so any process can resume it - `helix run`, `helix approve`
(1.8), or the API. Two suspension shapes exist: a mid-graph interrupt
leaves pending nodes the checkpointer resumes in place (invoke with no
input), while the approval gate ends the graph cleanly at
`awaiting_approval` - that resume re-enters at `intake` and the
coordinator decides from the persisted status plus the approvals row, so
it works even with no checkpointer at all. An incident in `failed`
re-enters via the failed -> diagnosing edge the coordinator owns. A run
that dies unexpectedly leaves the incident mid-status; the next
`helix run` picks it up the same way.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.graph import GraphDeps, build_graph, default_deps, open_checkpointer
from agent.state import WorkflowState
from state import models
from state.machine import IncidentStatus


class IncidentNotFoundError(LookupError):
    """No incident with that INC-* id."""


class RunConflictError(RuntimeError):
    """The incident already has a running workflow run."""


@dataclass
class RunResult:
    """What a run did - the runner's return to the CLI and the intake
    hook. `error` carries an unexpected run-level failure (the run row is
    then `failed`); expected domain failures are recorded in
    `state.errors` and surface through the incident status instead."""

    incident_id: str
    status: str  # incident status after the run
    run_status: models.RunStatus
    hypothesis: dict[str, Any] | None = None
    selected_action: dict[str, Any] | None = None  # 1.6: rank-1 candidate, if any
    execution: dict[str, Any] | None = None  # 1.8: latest executions-row summary
    verification: dict[str, Any] | None = None  # 1.9: latest verifications-row summary
    errors: list[str] = field(default_factory=list)
    error: str | None = None


def _load_incident(session: Session, incident_id: str) -> models.Incident | None:
    return session.scalar(select(models.Incident).where(models.Incident.incident_id == incident_id))


def start_run(session: Session, incident: models.Incident) -> models.WorkflowRun:
    """Open a `running` run row; refuses if one is already running.

    The partial unique index ux_workflow_runs_one_running is the
    concurrency backstop - this lookup is the friendly front door that
    produces a readable error instead of an IntegrityError."""
    running = session.scalar(
        select(models.WorkflowRun).where(
            models.WorkflowRun.incident_id == incident.id,
            models.WorkflowRun.status == models.RunStatus.RUNNING,
        )
    )
    if running is not None:
        raise RunConflictError(
            f"{incident.incident_id}: workflow run {running.id} is already running"
        )
    run = models.WorkflowRun(incident_id=incident.id, status=models.RunStatus.RUNNING)
    session.add(run)
    session.flush()
    return run


def finish_run(session: Session, run_id: int, status: models.RunStatus) -> None:
    """Close a run row. `suspended` keeps ended_at NULL - the run is
    parked, not over."""
    run = session.get(models.WorkflowRun, run_id)
    run.status = status
    run.ended_at = None if status == models.RunStatus.SUSPENDED else datetime.now(UTC)
    session.flush()


def _latest_hypothesis(session: Session, incident_pk: int) -> dict[str, Any] | None:
    row = session.scalar(
        select(models.Hypothesis)
        .where(models.Hypothesis.incident_id == incident_pk)
        .order_by(models.Hypothesis.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {
        "claim": row.claim,
        "evidence_refs": row.evidence_refs,
        "confidence": row.confidence,
        "contradicting_signals": row.contradicting_signals,
        "attempt": row.attempt,
    }


def _selected_candidate(session: Session, incident_pk: int) -> dict[str, Any] | None:
    """The candidate row the planner marked selected, latest attempt."""
    row = session.scalar(
        select(models.Candidate)
        .where(models.Candidate.incident_id == incident_pk)
        .where(models.Candidate.selected.is_(True))
        .order_by(models.Candidate.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {
        "action": row.action,
        "params": row.params,
        "rationale": row.rationale,
        "risk_tier": row.risk_tier,
        "score": row.score,
    }


def _latest_execution(session: Session, incident_pk: int) -> dict[str, Any] | None:
    """The newest executions row - the audit summary a resumed or
    approving caller wants to see."""
    row = session.scalar(
        select(models.Execution)
        .where(models.Execution.incident_id == incident_pk)
        .order_by(models.Execution.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {
        "execution_id": row.id,
        "action": row.action,
        "params": row.params,
        "fingerprint": row.fingerprint,
        "policy_decision": row.policy_decision,
        "result": row.result.value,
        "detail": row.detail,
        "approval_id": row.approval_id,
        "decided_by": row.decided_by,
        "origin": row.origin,
    }


def _latest_verification(session: Session, incident_pk: int) -> dict[str, Any] | None:
    """The newest verifications row - the desired-state verdict a caller
    wants to see (and the provenance record's verification stage)."""
    row = session.scalar(
        select(models.Verification)
        .where(models.Verification.incident_id == incident_pk)
        .order_by(models.Verification.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {
        "verification_id": row.id,
        "action": row.action,
        "outcome": row.outcome.value,
        "detail": row.detail,
        "checks": row.checks,
        "window_seconds": row.window_seconds,
        "needs_compensation": row.needs_compensation,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def run_incident(
    incident_id: str,
    *,
    deps: GraphDeps | None = None,
    arm: str | None = None,
) -> RunResult:
    """Drive `incident_id`'s workflow to its next stopping point.

    Raises IncidentNotFoundError for an unknown id and RunConflictError
    while another run is running. Unexpected graph failures mark the run
    `failed` and come back as RunResult.error rather than raising - the
    caller (CLI, intake hook) always gets a result it can report.
    """
    deps = deps or default_deps()
    with deps.session_scope() as session:
        incident = _load_incident(session, incident_id)
        if incident is None:
            raise IncidentNotFoundError(f"no such incident: {incident_id}")
        suspended = session.scalar(
            select(models.WorkflowRun)
            .where(models.WorkflowRun.incident_id == incident.id)
            .where(models.WorkflowRun.status == models.RunStatus.SUSPENDED)
            .order_by(models.WorkflowRun.id.desc())
            .limit(1)
        )
        if suspended is not None:
            # Resume in place: the run row goes back to running and the
            # graph continues from its checkpoint.
            run = suspended
            run.status = models.RunStatus.RUNNING
            run.ended_at = None
            session.flush()
        else:
            run = start_run(session, incident)
        run_id = run.id
        resuming = suspended is not None

    config = {"configurable": {"thread_id": incident_id}}
    final_state: WorkflowState = {}
    error: str | None = None
    interrupted = False
    try:
        with open_checkpointer(deps.database_url) as checkpointer:
            graph = build_graph(deps, arm=arm, checkpointer=checkpointer)
            invoke_input: WorkflowState | None = {"incident_id": incident_id}
            if resuming and checkpointer is not None:
                try:
                    # Pending nodes mean the earlier invoke parked mid-graph
                    # (interrupt_before) - resume it in place with None. A
                    # completed graph (the approval suspension ends cleanly
                    # at awaiting_approval) has nothing pending, so the
                    # resume re-enters at intake and the coordinator
                    # decides from the persisted status + approvals row.
                    has_pending = bool(graph.get_state(config).next)
                except ValueError:
                    has_pending = False
                invoke_input = None if has_pending else {"incident_id": incident_id}
            final_state = graph.invoke(invoke_input, config) or {}
            try:
                # Pending nodes mean the run parked mid-graph (interrupt /
                # approval gate) - that is a suspension, not an end.
                interrupted = bool(graph.get_state(config).next)
            except ValueError:
                interrupted = False  # no checkpointer: state lives only for the invoke
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    with deps.session_scope() as session:
        incident = _load_incident(session, incident_id)
        if error is not None:
            outcome = models.RunStatus.FAILED
        elif interrupted or incident.status == IncidentStatus.AWAITING_APPROVAL:
            outcome = models.RunStatus.SUSPENDED
        else:
            outcome = models.RunStatus.TERMINAL
        finish_run(session, run_id, outcome)
        hypothesis = _latest_hypothesis(session, incident.id)
        selected = _selected_candidate(session, incident.id)
        execution = _latest_execution(session, incident.id)
        verification = _latest_verification(session, incident.id)
        status = IncidentStatus(incident.status).value

    return RunResult(
        incident_id=incident_id,
        status=status,
        run_status=outcome,
        hypothesis=hypothesis,
        selected_action=selected,
        execution=execution,
        verification=verification,
        errors=list(final_state.get("errors", [])),
        error=error,
    )
