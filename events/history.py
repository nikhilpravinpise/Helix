"""Durable event history: query surface and replay (subphase E5).

`event_records` is the store - one table for both the vendor events E4
consumes and the lifecycle events E5 emits, because the Knowledge Agent
in E6 wants an incident's whole story rather than half of it.

## Replay, and what it can honestly reconstruct

The exit criterion is that incident state rebuilt purely from replayed
history matches the state produced by live processing. That has to be
stated precisely, because the taxonomy does not carry every workflow step.

Helix emits five lifecycle events. The intermediate statuses -
`diagnosing`, `validating`, `escalated`, `failed` - have no event in
CONTEXT.md section 7, and inventing some would be expanding the taxonomy
without a workflow that needs it. So an event log describes an incident's
**lifecycle**, not its every transition, and replay reconstructs exactly
that: the milestones reached, in order, plus the incident's identity.

`IncidentProjection` is therefore the unit of comparison, and it is built
two ways from genuinely different data:

- `project_from_state` reads the `incidents` row and its `transitions`.
- `project_from_events` reads nothing but the stored event envelopes.

Where they must agree is the interesting part: service, environment and
severity travel in event metadata on one path and in table columns on the
other, so agreement means the envelope really did carry the incident's
identity rather than a copy of the same query.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from events.lifecycle import event_type_for
from events.schemas import EngineeringEvent, EventSource, EventType
from state import models
from state.machine import IncidentStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from state.models import Incident

#: The workflow status each lifecycle milestone implies. Replay can place
#: an incident at these and nowhere else.
LIFECYCLE_STATUS: dict[EventType, IncidentStatus] = {
    EventType.INCIDENT_CREATED: IncidentStatus.RECEIVED,
    EventType.REMEDIATION_REQUESTED: IncidentStatus.REMEDIATION_PENDING,
    EventType.APPROVAL_REQUESTED: IncidentStatus.AWAITING_APPROVAL,
    EventType.REMEDIATION_COMPLETED: IncidentStatus.VERIFYING,
    EventType.INCIDENT_RESOLVED: IncidentStatus.RECOVERED,
}


@dataclass(frozen=True)
class IncidentProjection:
    """An incident's lifecycle, as reconstructible from its event log."""

    incident_id: str
    service: str
    environment: str
    severity: str
    #: Furthest lifecycle milestone reached, as a status.
    lifecycle_status: IncidentStatus
    #: Milestones in the order they were reached, deduplicated: re-entering
    #: `remediation_pending` on a replan is the same milestone again, and
    #: the event log records it once because the id derives from it.
    milestones: tuple[EventType, ...] = field(default_factory=tuple)


def _project(
    incident_id: str,
    service: str,
    environment: str,
    severity: str,
    milestones: Sequence[EventType],
) -> IncidentProjection:
    ordered: list[EventType] = []
    for milestone in milestones:
        if milestone not in ordered:
            ordered.append(milestone)
    status = LIFECYCLE_STATUS[ordered[-1]] if ordered else IncidentStatus.RECEIVED
    return IncidentProjection(
        incident_id=incident_id,
        service=service,
        environment=environment,
        severity=severity,
        lifecycle_status=status,
        milestones=tuple(ordered),
    )


def project_from_state(incident: "Incident") -> IncidentProjection:
    """The lifecycle projection of an incident's live state."""
    milestones = [
        event_type
        for transition in incident.transitions
        if (
            event_type := event_type_for(
                IncidentStatus(transition.from_status) if transition.from_status else None,
                IncidentStatus(transition.to_status),
            )
        )
        is not None
    ]
    return _project(
        incident.incident_id,
        incident.service,
        incident.environment,
        incident.severity,
        milestones,
    )


def project_from_events(events: Sequence[EngineeringEvent]) -> IncidentProjection | None:
    """The lifecycle projection rebuilt from an event log alone.

    Vendor events are ignored: they are what happened to the systems Helix
    watches, not what Helix did about it. Only Helix's own lifecycle events
    carry state.
    """
    lifecycle = [
        event
        for event in events
        if event.source is EventSource.HELIX and event.event_type in LIFECYCLE_STATUS
    ]
    if not lifecycle:
        return None
    lifecycle.sort(
        # timestamp alone is not a total order: two lifecycle events
        # emitted in the same clock tick compare equal, and a stable
        # sort then inherits input order - reversed input replays to a
        # different incident. The transition id in the envelope is
        # monotonic with the real transition order, so equal timestamps
        # resolve deterministically.
        key=lambda e: (e.timestamp, int((e.metadata or {}).get("transition_id") or 0))
    )
    first = lifecycle[0]
    metadata = first.metadata or {}
    return _project(
        str(metadata.get("incident") or first.correlation_id or ""),
        first.service,
        first.environment,
        str(metadata.get("severity") or "medium"),
        [event.event_type for event in lifecycle],
    )


# --- Query surface ------------------------------------------------------------
#
# E5 deliverable: "event history query surface for later use by the
# Knowledge Agent". Kept to reads that E6 retrieval will actually need -
# one incident's story, and a filtered scan across incidents.


def _to_events(rows: Sequence[models.EventRecord]) -> list[EngineeringEvent]:
    return [EngineeringEvent.model_validate(row.envelope) for row in rows]


def history_for_incident(
    session: Session, incident_id: str, *, source: EventSource | None = None
) -> list[EngineeringEvent]:
    """Every recorded event for one incident, oldest first."""
    stmt = (
        select(models.EventRecord)
        .join(models.Incident)
        .where(models.Incident.incident_id == incident_id)
        .order_by(models.EventRecord.occurred_at, models.EventRecord.id)
    )
    if source is not None:
        stmt = stmt.where(models.EventRecord.source == source.value)
    return _to_events(session.scalars(stmt).all())


def search_history(
    session: Session,
    *,
    service: str | None = None,
    environment: str | None = None,
    event_type: EventType | None = None,
    source: EventSource | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[EngineeringEvent]:
    """Filtered scan across the whole history, newest first.

    `limit` is capped rather than optional. An unbounded history query is
    the same trap `query-logs.sh` had: it succeeds on a small database and
    becomes a problem on a real one, silently.
    """
    stmt = select(models.EventRecord).order_by(
        models.EventRecord.occurred_at.desc(), models.EventRecord.id.desc()
    )
    if service is not None:
        stmt = stmt.where(models.EventRecord.service == service)
    if environment is not None:
        stmt = stmt.where(models.EventRecord.environment == environment)
    if event_type is not None:
        stmt = stmt.where(models.EventRecord.event_type == event_type.value)
    if source is not None:
        stmt = stmt.where(models.EventRecord.source == source.value)
    if since is not None:
        stmt = stmt.where(models.EventRecord.occurred_at >= since)
    if until is not None:
        stmt = stmt.where(models.EventRecord.occurred_at <= until)
    return _to_events(session.scalars(stmt.limit(max(1, limit))).all())


def replay_incident(session: Session, incident_id: str) -> IncidentProjection | None:
    """Rebuild an incident's lifecycle from its stored events alone."""
    return project_from_events(history_for_incident(session, incident_id))
