"""Helix HTTP API: signal intake and incident read endpoints (subphase 1.4).

The intake POSTs and `helix intake` both call state.intake.intake_signal,
so the benchmark harness never depends on this server running (D6, push
model). Auth: when HELIX_INTAKE_TOKEN is set, intake POSTs must carry
`Authorization: Bearer <token>`; unset means local-only trust. The read
endpoints are unauthenticated on that same local-only basis.

`helix serve` runs this app under uvicorn at HELIX_API_BIND:HELIX_API_PORT.
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from helix.config import get_settings
from state import models
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


@app.post("/intake/kubernetes", response_model=IntakeResult)
def post_intake_kubernetes(
    signal: Signal,
    session: DbSession,
    _: Annotated[None, Depends(require_intake_token)],
) -> IntakeResult:
    """Receive a Kubernetes failure signal (e.g. from scripts/emit-signal.sh)."""
    return _intake(signal, SignalSource.KUBERNETES, session)


@app.post("/intake/jenkins", response_model=IntakeResult)
def post_intake_jenkins(
    signal: Signal,
    session: DbSession,
    _: Annotated[None, Depends(require_intake_token)],
) -> IntakeResult:
    """Receive a Jenkins build-failure signal (Jenkinsfile post{failure})."""
    return _intake(signal, SignalSource.JENKINS, session)


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
