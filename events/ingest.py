"""Driving Helix state from the event stream (subphase E4).

`ingest_event` is the single entry point: it takes one canonical event and
either opens an incident or attaches the event to the one it correlates
with. The Kafka consumer in `run_consumer` is a thin loop around it, so
the same logic is testable without a broker - the same split
`state.intake` uses for the API and the CLI.

## Three guarantees

**Replay changes nothing.** Every event is recorded in `event_records`
under a UNIQUE index on `event_id`, and ingest looks that up first. A
redelivered event returns the incident it landed on the first time and
writes nothing. This is why E3 derives ids from the vendor payload rather
than generating them: without a stable id, replay would be indistinguishable
from a genuine second occurrence.

**Order does not matter.** Correlation is set-based - an event joins an
incident because it shares a token, not because it arrived in a particular
sequence - so the same events ingested in any order converge on the same
incident. The incident's `severity` and status are not driven backwards by
a late arrival, and `occurred_at` is stored separately from `created_at`
so out-of-order delivery is visible rather than smoothed over.

**Nothing executes.** Ingest opens incidents and attaches events. It never
advances an incident past RECEIVED and never triggers remediation, so a
replay of ten thousand events cannot re-execute a single action. Driving
the workflow stays an explicit `helix run`.

## What correlation does not do here

It does not merge two incidents that are already open. If a late event
carries tokens belonging to two distinct incidents, it attaches to the
oldest and its remaining tokens are recorded against that one. Merging
live incidents means reconciling two workflow states, two approval
histories and two sets of executions, which is a different problem from
correlation and is not one E4 needs to solve to meet its exit criterion.
The event's own tokens still bridge the two for anything reading the
token table.
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from events.correlation import CORRELATION_WINDOW, tokens_for
from events.schemas import EngineeringEvent, EventSource
from state import machine, models
from state.intake import _new_incident_id
from state.machine import IncidentStatus


class IngestResult(BaseModel):
    """What ingest did with one event."""

    incident_id: str
    #: True when this event opened a new incident.
    created: bool
    #: True when the event had already been recorded - nothing was written.
    duplicate: bool
    #: True when an existing incident was found by shared token rather than
    #: by this event having opened it.
    correlated: bool
    event_record_id: int | None = None


def _existing_record(session: Session, event_id: str) -> models.EventRecord | None:
    return session.scalar(select(models.EventRecord).where(models.EventRecord.event_id == event_id))


def _correlated_incident(
    session: Session, tokens: set[str], occurred_at: datetime
) -> models.Incident | None:
    """The open incident sharing a token with this event, if any.

    Oldest first, so a chain of related events keeps converging on the
    incident that started it rather than hopping to whichever one was
    touched last.
    """
    if not tokens:
        return None
    rows = session.scalars(
        select(models.CorrelationToken)
        .where(models.CorrelationToken.token.in_(sorted(tokens)))
        .order_by(models.CorrelationToken.incident_id)
    ).all()
    for row in rows:
        incident = row.incident
        if incident is None or incident.status in machine.TERMINAL_STATUSES:
            continue
        # The window is measured against the incident's most recent event,
        # not its creation: a long chain of related events legitimately
        # keeps an incident correlatable, while a forgotten one ages out.
        #
        # Helix's own lifecycle events (E5) are excluded. They are stamped
        # when Helix reacts, not when anything happened in the outside
        # world, so counting them would let an incident hold its
        # correlation window open by transitioning - and a stale incident
        # being walked through a replan would never age out at all.
        latest = max(
            (
                record.occurred_at
                for record in incident.event_records
                if record.source != EventSource.HELIX.value
            ),
            default=incident.created_at,
        )
        if latest is None:
            continue
        # Absolute difference, not signed. A signed check bounds only the
        # future: an event older than the incident's latest produces a
        # negative delta, which passes `<= CORRELATION_WINDOW` however far
        # back it is. Out-of-order delivery is exactly what a Kafka topic
        # produces, so that gap let a replayed event from a day earlier
        # attach to an unrelated open incident on nothing but the service
        # token. Measured on real captured fixtures: two faults 22 hours
        # apart merged into one incident when ingested newest-first.
        if abs(_aware(occurred_at) - _aware(latest)) <= CORRELATION_WINDOW:
            return incident
    return None


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes where Postgres keeps the offset;
    comparing the two raises. Normalize to UTC rather than forbidding the
    SQLite test engine."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _record_tokens(session: Session, incident: models.Incident, tokens: set[str]) -> None:
    """Attach any token this incident does not already carry."""
    known = {row.token for row in incident.correlation_tokens}
    for token in sorted(tokens - known):
        session.add(models.CorrelationToken(incident_id=incident.id, token=token))


def ingest_event(session: Session, event: EngineeringEvent) -> IngestResult:
    """Record one canonical event against the incident it belongs to.

    Flushes but never commits - the caller owns the transaction boundary,
    the same contract `state.intake.intake_signal` keeps.
    """
    seen = _existing_record(session, event.event_id)
    if seen is not None:
        # Replay. Return where it landed originally and write nothing.
        return IngestResult(
            incident_id=seen.incident.incident_id,
            created=False,
            duplicate=True,
            correlated=False,
            event_record_id=seen.id,
        )

    tokens = tokens_for(event)
    occurred_at = _aware(event.timestamp)
    incident = _correlated_incident(session, tokens, occurred_at)
    created = incident is None
    correlated = not created

    if incident is None:
        incident = models.Incident(
            incident_id=_new_incident_id(),
            # The event stream has no fault signature to fingerprint, so
            # the event id stands in. It is already unique and stable, and
            # it keeps the column's not-null contract honest rather than
            # inventing a fake signature.
            fingerprint=event.event_id,
            service=event.service,
            environment=event.environment,
            severity=str((event.metadata or {}).get("severity") or "medium"),
            status=IncidentStatus.RECEIVED,
        )
        session.add(incident)
        session.flush()
        machine.record_creation(
            incident,
            cause=f"event {event.event_type.value} from {event.source.value}",
            session=session,
        )

    _record_tokens(session, incident, tokens)
    record = models.EventRecord(
        incident_id=incident.id,
        event_id=event.event_id,
        event_type=event.event_type.value,
        source=event.source.value,
        service=event.service,
        environment=event.environment,
        occurred_at=occurred_at,
        envelope=event.model_dump(mode="json"),
    )
    session.add(record)
    session.flush()
    return IngestResult(
        incident_id=incident.incident_id,
        created=created,
        duplicate=False,
        correlated=correlated,
        event_record_id=record.id,
    )


def correlated_sources(session: Session, incident_id: str) -> set[str]:
    """Which EXTERNAL systems contributed events to an incident.

    The E4 exit criterion is stated in these terms - one incident with all
    four sources correlated - so it is worth being able to ask directly.

    Helix's own lifecycle events (E5) are excluded: they are not evidence
    that another system observed anything, and counting Helix as a
    correlated source would make every incident look one source richer
    than it is.
    """
    incident = session.scalar(
        select(models.Incident).where(models.Incident.incident_id == incident_id)
    )
    if incident is None:
        return set()
    return {
        record.source
        for record in incident.event_records
        if record.source != EventSource.HELIX.value
    }


def run_consumer(
    session_factory: Any,
    group_id: str = "helix-ingest",
    max_events: int | None = None,
    from_beginning: bool = False,
) -> int:
    """Consume canonical events and drive state. Returns events handled.

    A thin loop: every decision lives in `ingest_event`, which is why E4's
    behaviour is testable without a broker. Each event gets its own
    transaction, so one poison event cannot roll back the incidents its
    predecessors opened - and because ingest is idempotent, the redelivery
    that follows a mid-batch crash is harmless.
    """
    from events.broker import EventConsumer

    consumer = EventConsumer(group_id=group_id, from_beginning=from_beginning)

    def handle(event: EngineeringEvent) -> None:
        with session_factory() as session:
            ingest_event(session, event)

    try:
        return consumer.consume(handle, max_events=max_events)
    finally:
        consumer.close()
