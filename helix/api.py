"""Helix HTTP API: signal intake, incident reads, and approval decisions.

The intake POSTs and `helix intake` both call state.intake.intake_signal,
so the benchmark harness never depends on this server running (D6, push
model). Auth: when HELIX_INTAKE_TOKEN is set, intake POSTs and approval
decisions must carry `Authorization: Bearer <token>`; unset means
local-only trust. The read endpoints are unauthenticated on that same
local-only basis.

Subphase 1.8 adds the approval endpoints: `GET /approvals` lists the
gate's pending/decided rows, and `POST /approvals/{id}/approve|reject`
record the human decision (actor in the body) and resume the suspended
workflow run in-process - the decision is durable in Postgres first, the
resume is best-effort (`helix run` can always drive it later).

Subphase 1.9 adds `GET /incidents/{id}/provenance` - the assembled
decision-provenance record (state/provenance.py), the JSON surface the
Phase 1.12 operator timeline will render.

`helix serve` runs this app under uvicorn at HELIX_API_BIND:HELIX_API_PORT.
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.logging import get_logger
from state import models
from state.approvals import ApprovalDecidedError, record_decision
from state.db import session_scope
from state.intake import IntakeResult, Signal, SignalSource, intake_signal
from state.machine import IncidentStatus

app = FastAPI(
    title="Helix",
    description="Evidence-constrained DevOps recovery - intake and incident state",
)


def get_db_session() -> Iterator[Session]:
    """Request-scoped session; commits on clean exit, rolls back on error."""
    with session_scope() as session:
        yield session


DbSession = Annotated[Session, Depends(get_db_session)]


def require_intake_token(authorization: Annotated[str | None, Header()] = None) -> None:
    """Bearer-token gate for intake POSTs. No-op when HELIX_INTAKE_TOKEN is
    unset (local-only trust). Missing header -> 401, wrong token -> 403."""
    token = get_settings().intake_token
    if not token:
        return
    if authorization is None:
        raise HTTPException(
            status_code=401,
            detail="missing Authorization: Bearer <token> header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or credential != token:
        raise HTTPException(status_code=403, detail="invalid intake token")


def _intake(signal: Signal, expected_source: SignalSource, session: Session) -> IntakeResult:
    """Shared endpoint body: the path pins the source, so a signal whose
    declared source disagrees with the endpoint is a client error."""
    if signal.source != expected_source:
        raise HTTPException(
            status_code=422,
            detail=f"signal source {signal.source!r} does not match endpoint "
            f"{expected_source.value!r}",
        )
    return intake_signal(session, signal)


def _maybe_run(run: bool, session: Session, result: IntakeResult) -> None:
    """The intake -> agent hook (D6 direct call, wired in 1.5): POSTs with
    `?run=true` drive the workflow graph in-process after the signal is
    recorded. The signal is committed first so the runner's own sessions
    see it; a run failure is logged but never fails the intake - the
    incident is already durable and `helix run` can drive it later."""
    if not run:
        return
    session.commit()
    from agent.runner import run_incident

    try:
        run_incident(result.incident_id)
    except Exception as exc:  # the run's outcome lives in workflow_runs
        get_logger().warning(
            "intake run hook failed", incident_id=result.incident_id, error=str(exc)
        )


@app.post("/intake/kubernetes", response_model=IntakeResult)
def post_intake_kubernetes(
    signal: Signal,
    session: DbSession,
    _: Annotated[None, Depends(require_intake_token)],
    run: bool = False,
) -> IntakeResult:
    """Receive a Kubernetes failure signal (e.g. from scripts/emit-signal.sh).
    `?run=true` also drives the incident's workflow in-process."""
    result = _intake(signal, SignalSource.KUBERNETES, session)
    _maybe_run(run, session, result)
    return result


@app.post("/intake/jenkins", response_model=IntakeResult)
def post_intake_jenkins(
    signal: Signal,
    session: DbSession,
    _: Annotated[None, Depends(require_intake_token)],
    run: bool = False,
) -> IntakeResult:
    """Receive a Jenkins build-failure signal (Jenkinsfile post{failure}).
    `?run=true` also drives the incident's workflow in-process."""
    result = _intake(signal, SignalSource.JENKINS, session)
    _maybe_run(run, session, result)
    return result


class IncidentOut(BaseModel):
    """The canonical incident record, read side."""

    model_config = ConfigDict(from_attributes=True)

    incident_id: str
    service: str
    environment: str
    severity: str
    status: IncidentStatus
    attempt_count: int
    linked_pr: str | None
    linked_deployment: str | None
    fingerprint: str
    created_at: datetime
    updated_at: datetime
    signal_count: int = 0


class SignalOut(BaseModel):
    """One stored signal row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    environment: str
    severity: str
    signature: dict
    raw: dict
    created_at: datetime


class TransitionOut(BaseModel):
    """One persisted status transition. from_status is null only on the
    creation row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    from_status: IncidentStatus | None
    to_status: IncidentStatus
    cause: str
    created_at: datetime


class IncidentDetail(IncidentOut):
    """Incident plus its attached signals and transition timeline."""

    signals: list[SignalOut] = []
    transitions: list[TransitionOut] = []


@app.get("/incidents", response_model=list[IncidentOut])
def list_incidents(session: DbSession) -> list[IncidentOut]:
    """All incidents, newest first."""
    incidents = session.scalars(
        select(models.Incident).order_by(
            models.Incident.created_at.desc(), models.Incident.id.desc()
        )
    ).all()
    out = []
    for incident in incidents:
        item = IncidentOut.model_validate(incident)
        item.signal_count = len(incident.signals)
        out.append(item)
    return out


@app.get("/incidents/{incident_id}", response_model=IncidentDetail)
def get_incident(incident_id: str, session: DbSession) -> IncidentDetail:
    """One incident by its INC-* id, with signals and the transition
    timeline - the base of the 1.9 provenance record and the 1.12 UI."""
    incident = session.scalar(
        select(models.Incident).where(models.Incident.incident_id == incident_id)
    )
    if incident is None:
        raise HTTPException(status_code=404, detail=f"no such incident: {incident_id}")
    detail = IncidentDetail.model_validate(incident)
    detail.signal_count = len(detail.signals)
    return detail


@app.get("/incidents/{incident_id}/provenance")
def get_incident_provenance(incident_id: str, session: DbSession) -> dict:
    """The incident's decision-provenance record (subphase 1.9): the
    ordered trigger -> evidence -> hypothesis -> candidates -> decision ->
    validation -> approval -> execution -> verification timeline,
    assembled from stored rows by state/provenance.py. 404 on an unknown
    incident id."""
    from state.provenance import assemble_provenance

    record = assemble_provenance(session, incident_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no such incident: {incident_id}")
    return record


class ApprovalOut(BaseModel):
    """One approvals row - the gate's pending or decided record."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    incident_id: str  # the incident's INC-* external id
    action: str
    params: dict
    fingerprint: str
    status: models.ApprovalStatus
    decided_by: str | None
    decided_at: datetime | None
    note: str | None
    created_at: datetime


def _approval_out(row: models.Approval) -> ApprovalOut:
    return ApprovalOut(
        id=row.id,
        incident_id=row.incident.incident_id,
        action=row.action,
        params=row.params,
        fingerprint=row.fingerprint,
        status=row.status,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        note=row.note,
        created_at=row.created_at,
    )


@app.get("/approvals", response_model=list[ApprovalOut])
def list_approvals(
    session: DbSession,
    status: models.ApprovalStatus | None = None,
    incident_id: str | None = None,
) -> list[ApprovalOut]:
    """Approval rows, newest first; filterable by status / incident."""
    query = select(models.Approval).order_by(models.Approval.id.desc())
    if status is not None:
        query = query.where(models.Approval.status == status)
    if incident_id is not None:
        query = query.join(models.Incident).where(models.Incident.incident_id == incident_id)
    return [_approval_out(row) for row in session.scalars(query).all()]


class ApprovalDecisionIn(BaseModel):
    """The decision body: WHO approved or rejected. The approver identity
    is required - the audit record has no anonymous decisions."""

    actor: str = Field(min_length=1, max_length=128)
    note: str = ""


class ApprovalDecisionOut(BaseModel):
    """The decided row plus what the resumed run did."""

    approval: ApprovalOut
    resumed: bool
    status: str | None = None  # incident status after the resume
    run_status: str | None = None
    run_error: str | None = None


def _decide_approval(
    approval_id: int, approved: bool, body: ApprovalDecisionIn, session: Session
) -> ApprovalDecisionOut:
    """Record the decision on a pending row and resume the suspended run.

    The decision commits before `run_incident` fires so the runner's own
    sessions see it; a resume failure is reported in the response, never
    raised - the decision itself is already durable and `helix run` can
    drive the incident later."""
    approval = session.get(models.Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=f"no such approval: {approval_id}")
    try:
        record_decision(session, approval, approved, body.actor, body.note)
    except ApprovalDecidedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    incident_id = approval.incident.incident_id
    session.commit()

    resumed = False
    status = run_status = run_error = None
    from agent.runner import run_incident

    try:
        result = run_incident(incident_id)
        resumed = True
        status, run_status = result.status, result.run_status.value
        run_error = result.error
    except Exception as exc:  # decision is durable; the run is retryable
        run_error = f"{type(exc).__name__}: {exc}"
        get_logger().warning("approval resume failed", incident_id=incident_id, error=run_error)
    return ApprovalDecisionOut(
        approval=_approval_out(approval),
        resumed=resumed,
        status=status,
        run_status=run_status,
        run_error=run_error,
    )


@app.post("/approvals/{approval_id}/approve", response_model=ApprovalDecisionOut)
def approve_approval(
    approval_id: int,
    body: ApprovalDecisionIn,
    session: DbSession,
    _: Annotated[None, Depends(require_intake_token)],
) -> ApprovalDecisionOut:
    """Approve the pending row and resume the suspended run."""
    return _decide_approval(approval_id, True, body, session)


@app.post("/approvals/{approval_id}/reject", response_model=ApprovalDecisionOut)
def reject_approval(
    approval_id: int,
    body: ApprovalDecisionIn,
    session: DbSession,
    _: Annotated[None, Depends(require_intake_token)],
) -> ApprovalDecisionOut:
    """Reject the pending row and resume: the incident replans with the
    rejected fingerprint excluded."""
    return _decide_approval(approval_id, False, body, session)
