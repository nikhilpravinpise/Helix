"""Helix as an event producer (subphase E5).

Phase 1 consumed events; E5 makes Helix emit its own. The five lifecycle
types CONTEXT.md section 7 reserves for it are derived from status
transitions, which is the one place every state change already passes
through:

| Edge | Event |
|---|---|
| (creation) -> `received` | `incident.created` |
| any -> `remediation_pending` | `remediation.requested` |
| any -> `awaiting_approval` | `approval.requested` |
| `executing` -> `verifying` | `remediation.completed` |
| any -> `recovered` | `incident.resolved` |

`remediation.completed` is keyed on arriving at `verifying` rather than
on leaving `executing`, because `executing -> failed` is the transport
erroring and no remediation completed at all.

## Written, not published

Emission writes an `event_records` row inside the caller's transaction,
alongside the `transitions` row it describes. It does not talk to Kafka.

That is deliberate and it is the whole reason this design works. Every
state change in Helix passes through `state.machine.transition`, including
in unit tests against SQLite and in every Phase 1 workflow. Publishing
inline would make a running broker a prerequisite for all of it, and a
broker that is momentarily down would either lose the event or fail the
state change that caused it.

Instead the history store is the outbox: the event and the state change
commit or roll back together, so they can never disagree. `events.emit`
drains unpublished rows to Kafka afterwards, and because E3 gives every
event a stable id, draining twice is harmless.

## What is not emitted

The intermediate workflow statuses - `diagnosing`, `validating`,
`verifying`, `escalated`, `failed` - have no event in the taxonomy, and
CONTEXT.md section 7 says to expand only when a real workflow needs it.
So the event log describes an incident's lifecycle, not its every step,
and `events.history` is explicit that a replayed projection reproduces
the former rather than the latter.
"""

from typing import TYPE_CHECKING, Any

from events.schemas import EngineeringEvent, EventSource, EventType, derive_event_id
from state.machine import IncidentStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from state.models import Incident

#: Arriving at one of these statuses emits its event, whatever the edge
#: came from.
_ON_ARRIVAL: dict[IncidentStatus, EventType] = {
    IncidentStatus.REMEDIATION_PENDING: EventType.REMEDIATION_REQUESTED,
    IncidentStatus.AWAITING_APPROVAL: EventType.APPROVAL_REQUESTED,
    IncidentStatus.RECOVERED: EventType.INCIDENT_RESOLVED,
}

#: Edges where both ends matter.
_ON_EDGE: dict[tuple[IncidentStatus, IncidentStatus], EventType] = {
    (IncidentStatus.EXECUTING, IncidentStatus.VERIFYING): EventType.REMEDIATION_COMPLETED,
}


def event_type_for(
    from_status: IncidentStatus | None, to_status: IncidentStatus
) -> EventType | None:
    """The lifecycle event a transition emits, or None if it emits none."""
    if from_status is None:
        return EventType.INCIDENT_CREATED
    if (from_status, to_status) in _ON_EDGE:
        return _ON_EDGE[(from_status, to_status)]
    return _ON_ARRIVAL.get(to_status)


def build_lifecycle_event(
    incident: "Incident",
    event_type: EventType,
    cause: str,
    from_status: IncidentStatus | None,
    to_status: IncidentStatus,
) -> EngineeringEvent:
    """The canonical event for one lifecycle milestone.

    The id is derived from the incident and the milestone, so re-deriving
    an incident's events - during a replay, or after a crash mid-flush -
    produces the same ids rather than a second set.
    """
    metadata: dict[str, Any] = {
        "incident": incident.incident_id,
        "from_status": from_status.value if from_status else None,
        "to_status": to_status.value,
        "cause": cause,
        "severity": incident.severity,
    }
    return EngineeringEvent(
        event_id=derive_event_id("helix", incident.incident_id, event_type.value, to_status.value),
        event_type=event_type,
        source=EventSource.HELIX,
        # The incident this is about, which for Helix's own events is
        # always known - unlike a vendor event, which may arrive before
        # any incident exists.
        correlation_id=incident.incident_id,
        service=incident.service,
        environment=incident.environment,
        metadata=metadata,
    )


def emit_for_transition(
    incident: "Incident",
    *,
    from_status: IncidentStatus | None,
    to_status: IncidentStatus,
    cause: str,
    session: "Session",
) -> EngineeringEvent | None:
    """Record the lifecycle event for a transition, if it has one.

    Returns the event, or None when the transition is not a lifecycle
    milestone. Never raises on a duplicate: re-entering a status - a
    replan returning to `remediation_pending`, say - derives the same id,
    and the first record stands.
    """
    from sqlalchemy import select

    from state.models import EventRecord

    event_type = event_type_for(from_status, to_status)
    if event_type is None:
        return None

    event = build_lifecycle_event(incident, event_type, cause, from_status, to_status)
    existing = session.scalar(select(EventRecord).where(EventRecord.event_id == event.event_id))
    if existing is not None:
        return event

    session.add(
        EventRecord(
            incident_id=incident.id,
            event_id=event.event_id,
            event_type=event.event_type.value,
            source=event.source.value,
            service=event.service,
            environment=event.environment,
            occurred_at=event.timestamp,
            envelope=event.model_dump(mode="json"),
        )
    )
    return event
