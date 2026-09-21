"""Draining Helix's outbox to Kafka (subphase E5).

`events.lifecycle` writes each lifecycle event into `event_records` inside
the transaction that changed the state, so the two can never disagree.
This module is the second half: it produces those rows to the stream and
marks them published.

Split in two rather than publishing inline because every state change in
Helix passes through `state.machine.transition`, including in unit tests
and in every Phase 1 workflow. Publishing there would make a running
broker a prerequisite for all of it, and a broker that was briefly down
would either lose the event or fail the state change that caused it.
Neither is acceptable for a system whose job is not losing failures.

Re-draining is safe. Every event id is derived and stable, and E4's
consumer deduplicates on it, so a row published twice lands once.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from events.schemas import EngineeringEvent, EventSource
from state import models


def pending(session: Session, limit: int = 500) -> list[models.EventRecord]:
    """Unpublished events Helix produced, oldest first.

    Scoped to `source = helix`: rows consumed from the stream were never
    Helix's to publish, and echoing them back onto the topic they came
    from would be a loop.
    """
    return list(
        session.scalars(
            select(models.EventRecord)
            .where(models.EventRecord.published_at.is_(None))
            .where(models.EventRecord.source == EventSource.HELIX.value)
            .order_by(models.EventRecord.id)
            .limit(max(1, limit))
        ).all()
    )


def drain(session: Session, producer=None, limit: int = 500) -> int:
    """Publish pending lifecycle events. Returns how many were produced.

    Rows are marked published only after `flush()` returns, so a broker
    failure leaves them pending for the next run rather than silently
    dropping them.
    """
    rows = pending(session, limit=limit)
    if not rows:
        return 0

    if producer is None:
        from events.broker import EventProducer

        producer = EventProducer()

    for row in rows:
        producer.publish(EngineeringEvent.model_validate(row.envelope))
    producer.flush()

    published_at = datetime.now(UTC)
    for row in rows:
        row.published_at = published_at
    session.flush()
    return len(rows)
