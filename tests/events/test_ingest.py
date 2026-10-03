"""Correlation and consumption (subphase E4).

The exit criterion in two halves: a scenario spanning GitHub, Jenkins,
Kubernetes and ELK produces one incident with all four sources
correlated, and replaying the same events changes nothing.

Runs against the SQLite test engine, because every decision lives in
`ingest_event` rather than in the Kafka loop. The broker-backed path gets
one integration test at the end.
"""

from datetime import UTC, datetime, timedelta

import pytest

from events.correlation import CORRELATION_WINDOW, strong_tokens, tokens_for
from events.ingest import correlated_sources, ingest_event
from events.schemas import EngineeringEvent, EventSource, EventType
from state import models
from state.machine import IncidentStatus, transition

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
COMMIT = "77aa01c9e2b3f4d5a6b7c8d9e0f1a2b3c4d5e6f7"


def event(event_type, source, *, at=T0, service="payment-api", **metadata) -> EngineeringEvent:
    return EngineeringEvent(
        event_type=event_type,
        source=source,
        service=service,
        environment="sandbox",
        timestamp=at,
        metadata=metadata,
    )


#: The four-source chain the exit criterion names: a commit's check fails,
#: the build for it fails, the workload it deployed crashes, and that pod
#: logs an error. No pair shares every token - they chain.
def four_source_chain():
    return [
        event(EventType.BUILD_FAILED, EventSource.GITHUB, ref=COMMIT, check="payment-api-ci"),
        event(
            EventType.BUILD_FAILED,
            EventSource.JENKINS,
            at=T0 + timedelta(minutes=1),
            ref=COMMIT,
            job="payment-api",
            build_number=42,
        ),
        event(
            EventType.POD_FAILED,
            EventSource.KUBERNETES,
            at=T0 + timedelta(minutes=2),
            pod="payment-api-98867c74d-h6j8v",
            waiting_reason="CrashLoopBackOff",
        ),
        event(
            EventType.SERVICE_UNHEALTHY,
            EventSource.ELK,
            at=T0 + timedelta(minutes=3),
            pod="payment-api-98867c74d-h6j8v",
            message="ERROR:payment_api:startup failed",
        ),
    ]


class TestTokens:
    def test_service_and_environment_are_always_present(self):
        assert "service:payment-api:sandbox" in tokens_for(
            event(EventType.POD_FAILED, EventSource.KUBERNETES)
        )

    def test_commit_is_read_from_any_of_its_spellings(self):
        for key in ("ref", "sha", "commit", "head_sha"):
            tokens = tokens_for(event(EventType.BUILD_FAILED, EventSource.GITHUB, **{key: COMMIT}))
            assert f"commit:{COMMIT}" in tokens

    def test_build_token_needs_both_job_and_number(self):
        both = tokens_for(
            event(EventType.BUILD_FAILED, EventSource.JENKINS, job="j", build_number=1)
        )
        assert "build:j:1" in both
        partial = tokens_for(event(EventType.BUILD_FAILED, EventSource.JENKINS, job="j"))
        assert not any(t.startswith("build:") for t in partial)

    def test_build_number_zero_is_not_treated_as_missing(self):
        tokens = tokens_for(
            event(EventType.BUILD_FAILED, EventSource.JENKINS, job="j", build_number=0)
        )
        assert "build:j:0" in tokens

    def test_strong_tokens_exclude_proximity(self):
        """A correlation on identity is trustworthy outside the window; one
        on the service token is not."""
        tokens = strong_tokens(event(EventType.BUILD_FAILED, EventSource.GITHUB, ref=COMMIT, pr=17))
        assert tokens == {f"commit:{COMMIT}", "pr:17"}


class TestCorrelation:
    def test_four_sources_converge_on_one_incident(self, db_session):
        """The exit criterion. No two of these share all their tokens - the
        chain is commit, then service, then pod."""
        results = [ingest_event(db_session, e) for e in four_source_chain()]
        assert results[0].created is True
        assert [r.created for r in results[1:]] == [False, False, False]
        assert len({r.incident_id for r in results}) == 1
        assert correlated_sources(db_session, results[0].incident_id) == {
            "github",
            "jenkins",
            "kubernetes",
            "elk",
        }

    def test_correlation_is_order_independent(self, db_session):
        """Set-based correlation means delivery order cannot change the
        outcome - which is what makes out-of-order delivery a non-event."""
        shuffled = list(reversed(four_source_chain()))
        results = [ingest_event(db_session, e) for e in shuffled]
        assert len({r.incident_id for r in results}) == 1
        assert correlated_sources(db_session, results[0].incident_id) == {
            "github",
            "jenkins",
            "kubernetes",
            "elk",
        }

    def test_a_different_service_does_not_correlate(self, db_session):
        first = ingest_event(db_session, event(EventType.POD_FAILED, EventSource.KUBERNETES))
        second = ingest_event(
            db_session, event(EventType.POD_FAILED, EventSource.KUBERNETES, service="checkout-api")
        )
        assert first.incident_id != second.incident_id
        assert second.created is True

    def test_a_terminal_incident_does_not_absorb_new_events(self, db_session):
        """Same rule intake dedup already follows: a resolved failure
        surfacing again is a new occurrence."""
        first = ingest_event(db_session, event(EventType.POD_FAILED, EventSource.KUBERNETES))
        row = (
            db_session.query(models.Incident)
            .filter(models.Incident.incident_id == first.incident_id)
            .one()
        )
        transition(row, IncidentStatus.DIAGNOSING, cause="x", session=db_session)
        transition(row, IncidentStatus.ESCALATED, cause="done", session=db_session)
        db_session.flush()

        second = ingest_event(
            db_session,
            event(EventType.POD_FAILED, EventSource.KUBERNETES, at=T0 + timedelta(minutes=1)),
        )
        assert second.created is True
        assert second.incident_id != first.incident_id

    def test_an_event_outside_the_window_starts_a_new_incident(self, db_session):
        """Without this, a stale open incident - a documented occurrence
        after a killed run - would silently swallow every later event for
        its service, which looks exactly like correlation working."""
        first = ingest_event(db_session, event(EventType.POD_FAILED, EventSource.KUBERNETES))
        late = event(
            EventType.POD_FAILED,
            EventSource.KUBERNETES,
            at=T0 + CORRELATION_WINDOW + timedelta(minutes=1),
        )
        second = ingest_event(db_session, late)
        assert second.created is True
        assert second.incident_id != first.incident_id

    def test_an_event_inside_the_window_still_correlates(self, db_session):
        first = ingest_event(db_session, event(EventType.POD_FAILED, EventSource.KUBERNETES))
        second = ingest_event(
            db_session,
            event(
                EventType.POD_FAILED,
                EventSource.KUBERNETES,
                at=T0 + CORRELATION_WINDOW - timedelta(minutes=1),
            ),
        )
        assert second.correlated is True
        assert second.incident_id == first.incident_id

    def test_the_window_tracks_the_latest_event_not_the_first(self, db_session):
        """A long chain of related events legitimately keeps an incident
        correlatable; only a forgotten one ages out."""
        step = CORRELATION_WINDOW - timedelta(minutes=1)
        ids = set()
        for i in range(4):
            result = ingest_event(
                db_session,
                event(EventType.POD_FAILED, EventSource.KUBERNETES, at=T0 + step * i),
            )
            ids.add(result.incident_id)
        assert len(ids) == 1

    def test_tokens_are_recorded_once_per_incident(self, db_session):
        for e in four_source_chain():
            ingest_event(db_session, e)
        rows = db_session.scalars(models.CorrelationToken.__table__.select()).all()
        tokens = [r.token for r in db_session.query(models.CorrelationToken).all()]
        assert len(tokens) == len(set(tokens)), "a token was stored twice for one incident"
        assert rows is not None


class TestReplay:
    def test_replaying_an_event_changes_nothing(self, db_session):
        """The second half of the exit criterion."""
        events = four_source_chain()
        for e in events:
            ingest_event(db_session, e)

        before_incidents = db_session.query(models.Incident).count()
        before_records = db_session.query(models.EventRecord).count()
        before_tokens = db_session.query(models.CorrelationToken).count()
        before_transitions = db_session.query(models.Transition).count()

        replayed = [ingest_event(db_session, e) for e in events]

        assert all(r.duplicate for r in replayed)
        assert db_session.query(models.Incident).count() == before_incidents
        assert db_session.query(models.EventRecord).count() == before_records
        assert db_session.query(models.CorrelationToken).count() == before_tokens
        assert db_session.query(models.Transition).count() == before_transitions

    def test_replay_reports_the_incident_it_originally_landed_on(self, db_session):
        """The same event, not merely an identical-looking one. Two events
        built separately carry different generated ids and are two
        occurrences; a real redelivery reuses the id the adapter derived
        from the vendor payload, which is what makes it a replay."""
        original = event(EventType.POD_FAILED, EventSource.KUBERNETES)
        first = ingest_event(db_session, original)
        again = ingest_event(db_session, original)
        assert again.duplicate is True
        assert again.incident_id == first.incident_id

    def test_a_structurally_identical_event_with_a_new_id_is_not_a_replay(self, db_session):
        """Deduplication is on identity, not on shape - two genuine
        occurrences of the same fault must both be recorded."""
        first = ingest_event(db_session, event(EventType.POD_FAILED, EventSource.KUBERNETES))
        second = ingest_event(db_session, event(EventType.POD_FAILED, EventSource.KUBERNETES))
        assert second.duplicate is False
        assert second.correlated is True
        assert second.incident_id == first.incident_id
        # Scoped to vendor events: the incident also carries Helix's own
        # incident.created lifecycle record (E5), in the same history.
        assert (
            db_session.query(models.EventRecord)
            .filter(models.EventRecord.source != "helix")
            .count()
            == 2
        )

    def test_replaying_the_whole_stream_many_times_is_stable(self, db_session):
        events = four_source_chain()
        for _ in range(5):
            for e in events:
                ingest_event(db_session, e)
        assert db_session.query(models.Incident).count() == 1
        assert db_session.query(models.EventRecord).filter(
            models.EventRecord.source != "helix"
        ).count() == len(events)

    def test_ingest_never_advances_an_incident(self, db_session):
        """Replay must not be able to re-execute anything, and the reason
        it cannot is that ingest does not drive the workflow at all."""
        for e in four_source_chain():
            ingest_event(db_session, e)
        incident = db_session.query(models.Incident).one()
        assert incident.status is IncidentStatus.RECEIVED
        assert len(incident.transitions) == 1
        assert incident.executions == []


class TestRecordedEnvelope:
    def test_the_whole_event_is_kept(self, db_session):
        """E5 builds history and replay on this column, so storing only the
        fields E4 reads would force a re-ingest to recover the rest."""
        original = event(EventType.POD_FAILED, EventSource.KUBERNETES, pod="p", waiting_reason="x")
        ingest_event(db_session, original)
        record = (
            db_session.query(models.EventRecord).filter(models.EventRecord.source != "helix").one()
        )
        assert EngineeringEvent.model_validate(record.envelope) == original

    def test_occurred_at_is_kept_apart_from_consumed_at(self, db_session):
        """Out-of-order delivery makes the two disagree, and both matter."""
        past = event(EventType.POD_FAILED, EventSource.KUBERNETES, at=T0)
        ingest_event(db_session, past)
        record = (
            db_session.query(models.EventRecord).filter(models.EventRecord.source != "helix").one()
        )
        assert record.occurred_at.replace(tzinfo=UTC) == T0
        assert record.created_at is not None


@pytest.mark.integration
class TestConsumerAgainstKafka:
    """The same ingest path, driven by the real broker."""

    def test_events_published_to_kafka_drive_state(self, pg):
        import uuid

        from confluent_kafka.admin import AdminClient, NewTopic

        from events.broker import EventConsumer, EventProducer
        from state.db import session_scope

        bootstrap = "127.0.0.1:9092"
        try:
            AdminClient({"bootstrap.servers": bootstrap}).list_topics(timeout=5)
        except Exception:
            pytest.skip("kafka not reachable - run scripts/kafka-up.sh")

        suffix = uuid.uuid4().hex[:8]
        topic, dlq = f"test.e4.{suffix}", f"test.e4dlq.{suffix}"
        admin = AdminClient({"bootstrap.servers": bootstrap})
        for future in admin.create_topics(
            [
                NewTopic(topic, num_partitions=1, replication_factor=1),
                NewTopic(dlq, num_partitions=1, replication_factor=1),
            ]
        ).values():
            future.result(timeout=15)

        try:
            chain = four_source_chain()
            producer = EventProducer(bootstrap_servers=bootstrap, topic=topic)
            producer.publish_many(chain)
            producer.flush()

            consumer = EventConsumer(
                group_id=f"e4-{suffix}",
                bootstrap_servers=bootstrap,
                topic=topic,
                dlq_topic=dlq,
                from_beginning=True,
            )
            seen = []
            try:
                consumer.consume(
                    lambda e: seen.append(_ingest_committed(pg, e)), max_events=len(chain)
                )
            finally:
                consumer.close()

            assert len({r.incident_id for r in seen}) == 1
            with session_scope(pg) as session:
                assert correlated_sources(session, seen[0].incident_id) == {
                    "github",
                    "jenkins",
                    "kubernetes",
                    "elk",
                }
        finally:
            for future in admin.delete_topics([topic, dlq]).values():
                try:
                    future.result(timeout=15)
                except Exception:
                    pass


def _ingest_committed(engine, event_):
    from state.db import session_scope

    with session_scope(engine) as session:
        return ingest_event(session, event_)


class TestKubernetesEventIdentity:
    """A Kubernetes Warning Event and the object it is about must share an
    identity token, not merely the service token."""

    def test_a_warning_about_a_pod_shares_the_pods_token(self):
        """They were spelled `object:<name>` and `pod:<name>`, so nothing
        but the service token linked them - invisible on a single-service
        sandbox, and a split incident on any cluster where the service
        token is actually discriminating."""
        pod = event(EventType.POD_FAILED, EventSource.KUBERNETES, pod="payment-api-abc")
        warning = event(
            EventType.POD_FAILED,
            EventSource.KUBERNETES,
            involved_kind="Pod",
            involved_name="payment-api-abc",
        )
        shared = strong_tokens(pod) & strong_tokens(warning)
        assert shared == {"pod:payment-api-abc"}

    def test_a_warning_about_a_non_pod_keeps_the_object_token(self):
        warning = event(
            EventType.DEPLOYMENT_FAILED,
            EventSource.KUBERNETES,
            involved_kind="Deployment",
            involved_name="payment-api",
        )
        assert "object:payment-api" in strong_tokens(warning)

    def test_they_correlate_without_relying_on_the_service_token(self, db_session):
        """The behavioural half: correlation must survive even when the
        service token cannot do the work."""
        pod = event(EventType.POD_FAILED, EventSource.KUBERNETES, pod="payment-api-abc")
        warning = event(
            EventType.POD_FAILED,
            EventSource.KUBERNETES,
            at=T0 + timedelta(minutes=1),
            service="other-service",
            involved_kind="Pod",
            involved_name="payment-api-abc",
        )
        first = ingest_event(db_session, pod)
        second = ingest_event(db_session, warning)
        assert second.correlated is True
        assert second.incident_id == first.incident_id


class TestConsumerRunHook:
    """`helix consume --run` drives incidents from the stream through an
    after-commit hook on run_consumer. Ingest itself still never advances
    an incident (D19); the hook is the explicit opt-in, the way
    `helix intake --run` is for signals."""

    def _fake_consumer(self, monkeypatch, events):
        class FakeConsumer:
            def __init__(self, **kwargs):
                pass

            def consume(self, handler, max_events=None, idle_rounds=3):
                for item in events:
                    handler(item)
                return len(events)

            def close(self):
                pass

        monkeypatch.setattr("events.broker.EventConsumer", FakeConsumer)

    def test_hook_sees_each_new_event_after_its_transaction_commits(
        self, sqlite_engine, monkeypatch
    ):
        import functools

        from events.ingest import run_consumer
        from state.db import session_scope

        first = event(EventType.POD_FAILED, EventSource.KUBERNETES, pod="payment-api-1")
        self._fake_consumer(monkeypatch, [first])
        seen = []

        def hook(result):
            # After commit: a fresh session already sees the incident.
            with session_scope(sqlite_engine) as session:
                found = (
                    session.query(models.Incident)
                    .filter_by(incident_id=result.incident_id)
                    .one_or_none()
                )
            seen.append((result.incident_id, found is not None))

        run_consumer(functools.partial(session_scope, sqlite_engine), on_ingested=hook)
        assert len(seen) == 1 and seen[0][1] is True

    def test_a_redelivered_event_never_reaches_the_hook(self, sqlite_engine, monkeypatch):
        """D19's whole reason: replaying an event must not re-execute an
        action. A duplicate writes nothing and triggers nothing."""
        import functools

        from events.ingest import run_consumer
        from state.db import session_scope

        once = event(EventType.POD_FAILED, EventSource.KUBERNETES, pod="payment-api-1")
        self._fake_consumer(monkeypatch, [once, once])
        seen = []
        run_consumer(
            functools.partial(session_scope, sqlite_engine),
            on_ingested=lambda result: seen.append(result.incident_id),
        )
        assert len(seen) == 1
