"""Helix event emission, history and replay (subphase E5).

The exit criterion: incident state rebuilt purely from replayed history
matches the state produced by live processing. `IncidentProjection` is the
unit of comparison, built from the incidents/transitions tables on one
side and from stored event envelopes on the other.
"""

from datetime import UTC, datetime, timedelta

import pytest

from events.emit import drain, pending
from events.history import (
    LIFECYCLE_STATUS,
    history_for_incident,
    project_from_events,
    project_from_state,
    replay_incident,
    search_history,
)
from events.lifecycle import event_type_for
from events.schemas import EngineeringEvent, EventSource, EventType
from state import models
from state.intake import intake_signal
from state.machine import IncidentStatus, transition

K8S_SIGNAL = {
    "source": "kubernetes",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "high",
    "signature": {"waiting_reason": "CrashLoopBackOff", "container": "payment-api"},
}

#: The happy path through the Phase 1 state machine, ending recovered.
FULL_RUN = [
    (IncidentStatus.DIAGNOSING, "diagnose"),
    (IncidentStatus.REMEDIATION_PENDING, "plan"),
    (IncidentStatus.VALIDATING, "validate"),
    (IncidentStatus.AWAITING_APPROVAL, "policy requires approval"),
    (IncidentStatus.EXECUTING, "approved"),
    (IncidentStatus.VERIFYING, "executed"),
    (IncidentStatus.RECOVERED, "verified"),
]


def drive(session, incident, steps=FULL_RUN):
    for status, cause in steps:
        transition(incident, status, cause=cause, session=session)
    session.flush()
    return incident


def open_incident(session, signal=None):
    result = intake_signal(session, signal or K8S_SIGNAL)
    return (
        session.query(models.Incident)
        .filter(models.Incident.incident_id == result.incident_id)
        .one()
    )


class TestEventTypeMapping:
    def test_creation_emits_incident_created(self):
        assert event_type_for(None, IncidentStatus.RECEIVED) is EventType.INCIDENT_CREATED

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (IncidentStatus.REMEDIATION_PENDING, EventType.REMEDIATION_REQUESTED),
            (IncidentStatus.AWAITING_APPROVAL, EventType.APPROVAL_REQUESTED),
            (IncidentStatus.RECOVERED, EventType.INCIDENT_RESOLVED),
        ],
    )
    def test_arrival_at_a_milestone_emits_its_event(self, status, expected):
        assert event_type_for(IncidentStatus.DIAGNOSING, status) is expected

    def test_remediation_completed_is_keyed_on_reaching_verifying(self):
        """`executing -> failed` is the transport erroring, and no
        remediation completed at all."""
        assert (
            event_type_for(IncidentStatus.EXECUTING, IncidentStatus.VERIFYING)
            is EventType.REMEDIATION_COMPLETED
        )
        assert event_type_for(IncidentStatus.EXECUTING, IncidentStatus.FAILED) is None

    @pytest.mark.parametrize(
        "status",
        [IncidentStatus.DIAGNOSING, IncidentStatus.VALIDATING, IncidentStatus.ESCALATED],
    )
    def test_intermediate_statuses_emit_nothing(self, status):
        """They have no event in the taxonomy, and inventing one would be
        expanding it without a workflow that needs it."""
        assert event_type_for(IncidentStatus.RECEIVED, status) is None


class TestEmission:
    def test_intake_emits_incident_created(self, db_session):
        incident = open_incident(db_session)
        events = history_for_incident(db_session, incident.incident_id, source=EventSource.HELIX)
        assert [e.event_type for e in events] == [EventType.INCIDENT_CREATED]
        assert events[0].correlation_id == incident.incident_id

    def test_a_full_run_emits_all_five(self, db_session):
        incident = drive(db_session, open_incident(db_session))
        events = history_for_incident(db_session, incident.incident_id, source=EventSource.HELIX)
        assert [e.event_type for e in events] == [
            EventType.INCIDENT_CREATED,
            EventType.REMEDIATION_REQUESTED,
            EventType.APPROVAL_REQUESTED,
            EventType.REMEDIATION_COMPLETED,
            EventType.INCIDENT_RESOLVED,
        ]

    def test_events_carry_the_incident_identity(self, db_session):
        incident = open_incident(db_session)
        event = history_for_incident(db_session, incident.incident_id, source=EventSource.HELIX)[0]
        assert event.service == "payment-api"
        assert event.environment == "sandbox"
        assert event.metadata["severity"] == "high"
        assert event.metadata["to_status"] == "received"

    def test_the_event_lands_in_the_same_transaction_as_the_transition(self, db_session):
        """The outbox property: the event and the state change it describes
        commit or roll back together, so they cannot disagree."""
        open_incident(db_session)
        db_session.rollback()
        assert db_session.query(models.EventRecord).count() == 0
        assert db_session.query(models.Transition).count() == 0

    def test_re_entering_a_status_emits_its_own_event(self, db_session):
        """A replan returns to remediation_pending: that is a REAL second
        transition, and the event stream must record it - the id derives
        from the durable transition row, not the milestone alone, so a
        repeat arrival emits its own event rather than collapsing into
        the first. (The earlier milestone-keyed derivation dropped the
        second review.requested of a reject-fix-rereview cycle entirely -
        the stream told an incomplete story.)"""
        incident = open_incident(db_session)
        drive(
            db_session,
            incident,
            [
                (IncidentStatus.DIAGNOSING, "d"),
                (IncidentStatus.REMEDIATION_PENDING, "plan"),
                (IncidentStatus.VALIDATING, "v"),
                (IncidentStatus.REMEDIATION_PENDING, "replan"),
            ],
        )
        events = history_for_incident(db_session, incident.incident_id, source=EventSource.HELIX)
        assert [e.event_type for e in events].count(EventType.REMEDIATION_REQUESTED) == 2

    def test_helix_events_do_not_become_correlation_tokens(self, db_session):
        """Emission writes history directly; it does not re-enter ingest,
        so it cannot widen an incident's correlation surface."""
        drive(db_session, open_incident(db_session))
        assert db_session.query(models.CorrelationToken).count() == 0


class TestReplay:
    def test_replayed_projection_matches_live_state(self, db_session):
        """The exit criterion."""
        incident = drive(db_session, open_incident(db_session))
        live = project_from_state(incident)
        replayed = replay_incident(db_session, incident.incident_id)
        assert replayed == live

    def test_replay_reaches_the_terminal_status(self, db_session):
        incident = drive(db_session, open_incident(db_session))
        replayed = replay_incident(db_session, incident.incident_id)
        assert replayed.lifecycle_status is IncidentStatus.RECOVERED

    def test_replay_of_a_partial_run_matches(self, db_session):
        """An incident still in flight replays to where it has got to, not
        to where it will end up."""
        incident = open_incident(db_session)
        drive(
            db_session,
            incident,
            [(IncidentStatus.DIAGNOSING, "d"), (IncidentStatus.REMEDIATION_PENDING, "plan")],
        )
        replayed = replay_incident(db_session, incident.incident_id)
        assert replayed == project_from_state(incident)
        assert replayed.lifecycle_status is IncidentStatus.REMEDIATION_PENDING

    def test_identity_survives_replay(self, db_session):
        """The non-circular part: service, environment and severity travel
        in event metadata on the replay path and in table columns on the
        live path, so agreeing means the envelope really carried them."""
        incident = drive(db_session, open_incident(db_session))
        replayed = replay_incident(db_session, incident.incident_id)
        assert (replayed.service, replayed.environment, replayed.severity) == (
            "payment-api",
            "sandbox",
            "high",
        )

    def test_vendor_events_do_not_affect_the_projection(self, db_session):
        """They record what happened to the systems Helix watches, not what
        Helix did about it."""
        from events.ingest import ingest_event

        vendor = EngineeringEvent(
            event_type=EventType.POD_FAILED,
            source=EventSource.KUBERNETES,
            service="payment-api",
            environment="sandbox",
            metadata={"pod": "p"},
        )
        ingest_event(db_session, vendor)
        incident = db_session.query(models.Incident).order_by(models.Incident.id.desc()).first()
        drive(db_session, incident)
        assert replay_incident(db_session, incident.incident_id) == project_from_state(incident)

    def test_an_incident_with_no_lifecycle_events_replays_to_nothing(self):
        assert project_from_events([]) is None

    def test_replay_is_order_independent(self, db_session):
        """Events are sorted by their own timestamp, so the order they come
        back from storage cannot change the result."""
        incident = drive(db_session, open_incident(db_session))
        events = history_for_incident(db_session, incident.incident_id)
        assert project_from_events(list(reversed(events))) == project_from_events(events)

    def test_same_timestamp_replay_is_still_order_independent(self, db_session):
        """Two lifecycle events sharing one clock tick must still replay
        to the same incident regardless of storage order - the envelope's
        transition id, not input order, breaks the tie."""
        incident = drive(db_session, open_incident(db_session))
        events = history_for_incident(db_session, incident.incident_id)
        tick = min(e.timestamp for e in events)
        flattened = [e.model_copy(update={"timestamp": tick}) for e in events]
        assert project_from_events(list(reversed(flattened))) == project_from_events(flattened)
        # And the flattened replay still reaches the incident's real end
        # state - the tiebreak order is the TRUE transition order.
        assert project_from_events(flattened).lifecycle_status == incident.status

    def test_every_lifecycle_type_has_a_status(self):
        """A milestone with no status would make replay silently incomplete."""
        emitted = {
            EventType.INCIDENT_CREATED,
            EventType.REMEDIATION_REQUESTED,
            EventType.APPROVAL_REQUESTED,
            EventType.REMEDIATION_COMPLETED,
            EventType.INCIDENT_RESOLVED,
        }
        assert emitted == set(LIFECYCLE_STATUS)


class TestQuerySurface:
    def test_history_for_incident_is_oldest_first(self, db_session):
        incident = drive(db_session, open_incident(db_session))
        events = history_for_incident(db_session, incident.incident_id)
        assert [e.timestamp for e in events] == sorted(e.timestamp for e in events)

    def test_history_can_be_filtered_by_source(self, db_session):
        incident = drive(db_session, open_incident(db_session))
        helix_only = history_for_incident(
            db_session, incident.incident_id, source=EventSource.HELIX
        )
        assert {e.source for e in helix_only} == {EventSource.HELIX}

    def test_search_filters_by_type(self, db_session):
        drive(db_session, open_incident(db_session))
        found = search_history(db_session, event_type=EventType.INCIDENT_RESOLVED)
        assert [e.event_type for e in found] == [EventType.INCIDENT_RESOLVED]

    def test_search_filters_by_service(self, db_session):
        drive(db_session, open_incident(db_session))
        assert search_history(db_session, service="payment-api")
        assert search_history(db_session, service="checkout-api") == []

    def test_search_filters_by_time_range(self, db_session):
        drive(db_session, open_incident(db_session))
        future = datetime.now(UTC) + timedelta(days=1)
        assert search_history(db_session, since=future) == []
        assert search_history(db_session, until=future)

    def test_search_is_bounded(self, db_session):
        """An unbounded history query succeeds on a small database and
        becomes a problem on a real one, silently."""
        drive(db_session, open_incident(db_session))
        assert len(search_history(db_session, limit=2)) == 2

    def test_search_returns_newest_first(self, db_session):
        drive(db_session, open_incident(db_session))
        found = search_history(db_session)
        assert [e.timestamp for e in found] == sorted((e.timestamp for e in found), reverse=True)


class TestOutboxDrain:
    def test_lifecycle_events_start_unpublished(self, db_session):
        drive(db_session, open_incident(db_session))
        assert len(pending(db_session)) == 5

    def test_consumed_events_are_never_drained(self, db_session):
        """Rows consumed from the stream were never Helix's to publish, and
        echoing them back onto the topic they came from would be a loop."""
        from events.ingest import ingest_event

        ingest_event(
            db_session,
            EngineeringEvent(
                event_type=EventType.POD_FAILED,
                source=EventSource.KUBERNETES,
                service="payment-api",
                environment="sandbox",
                metadata={"pod": "p"},
            ),
        )
        assert all(row.source == "helix" for row in pending(db_session))

    def test_drain_publishes_and_marks(self, db_session):
        drive(db_session, open_incident(db_session))
        produced = []

        class FakeProducer:
            def publish(self, event):
                produced.append(event)

            def flush(self, timeout=10.0):
                pass

        assert drain(db_session, producer=FakeProducer()) == 5
        assert len(produced) == 5
        assert pending(db_session) == []

    def test_a_broker_failure_leaves_rows_pending(self, db_session):
        """Marked published only after flush returns, so a failure retries
        rather than silently dropping the event."""
        drive(db_session, open_incident(db_session))

        class BrokenProducer:
            def publish(self, event):
                pass

            def flush(self, timeout=10.0):
                raise RuntimeError("broker down")

        with pytest.raises(RuntimeError, match="broker down"):
            drain(db_session, producer=BrokenProducer())
        assert len(pending(db_session)) == 5

    def test_draining_twice_publishes_once(self, db_session):
        drive(db_session, open_incident(db_session))

        class FakeProducer:
            def __init__(self):
                self.count = 0

            def publish(self, event):
                self.count += 1

            def flush(self, timeout=10.0):
                pass

        producer = FakeProducer()
        assert drain(db_session, producer=producer) == 5
        assert drain(db_session, producer=producer) == 0
        assert producer.count == 5

    def test_nothing_to_drain_is_not_an_error(self, db_session):
        assert drain(db_session, producer=object()) == 0
