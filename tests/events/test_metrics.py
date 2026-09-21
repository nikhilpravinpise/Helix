"""What the event layer bought (subphase E7).

Each metric is exercised on: a clean pass, a case it should honestly
report as unmeasurable rather than pass or fail, and the zero/empty-input
guard against division by zero. `event_layer_report` is checked for shape
and for rolling the same numbers up rather than recomputing them
differently.
"""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine

from events.ingest import ingest_event
from events.metrics import (
    correlation_accuracy,
    duplicate_handling_rate,
    event_latency,
    event_layer_report,
    main,
    render_report_text,
    replay_correctness,
)
from events.schemas import EngineeringEvent, EventSource, EventType
from state import models
from state.intake import intake_signal
from state.machine import IncidentStatus, transition

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
COMMIT = "77aa01c9e2b3f4d5a6b7c8d9e0f1a2b3c4d5e6f7"

K8S_SIGNAL = {
    "source": "kubernetes",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "high",
    "signature": {"waiting_reason": "CrashLoopBackOff", "container": "payment-api"},
}


def event(event_type, source, *, at=T0, service="payment-api", **metadata) -> EngineeringEvent:
    return EngineeringEvent(
        event_type=event_type,
        source=source,
        service=service,
        environment="sandbox",
        timestamp=at,
        metadata=metadata,
    )


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


def open_incident(session, signal=None):
    result = intake_signal(session, signal or K8S_SIGNAL)
    return (
        session.query(models.Incident)
        .filter(models.Incident.incident_id == result.incident_id)
        .one()
    )


def bare_incident(session, incident_id="inc_bare") -> models.Incident:
    """An incident row with no transitions and no lifecycle events -
    inserted directly, bypassing `record_creation`, to model data that
    predates event emission or arrived by some other path."""
    incident = models.Incident(
        incident_id=incident_id,
        fingerprint="fp",
        service="payment-api",
        environment="sandbox",
        severity="medium",
        status=IncidentStatus.RECEIVED,
    )
    session.add(incident)
    session.flush()
    return incident


class TestCorrelationAccuracy:
    def test_a_correctly_correlated_group_is_counted(self, db_session):
        chain = four_source_chain()
        for e in chain:
            ingest_event(db_session, e)
        result = correlation_accuracy(db_session, [[e.event_id for e in chain]])
        assert result.rate == 1.0
        assert result.correct == 1
        assert result.denominator == 1
        assert result.groups[0].correct is True
        assert result.groups[0].missing_event_ids == ()

    def test_a_group_that_did_not_correlate_is_counted_wrong(self, db_session):
        """Two events deliberately kept from sharing any token - a
        different service - so they land on different incidents."""
        first = event(EventType.POD_FAILED, EventSource.KUBERNETES)
        second = event(EventType.POD_FAILED, EventSource.KUBERNETES, service="checkout-api")
        ingest_event(db_session, first)
        ingest_event(db_session, second)
        result = correlation_accuracy(db_session, [[first.event_id, second.event_id]])
        assert result.rate == 0.0
        assert result.correct == 0
        assert result.groups[0].correct is False
        assert len(set(result.groups[0].incident_ids)) == 2

    def test_an_event_id_that_was_never_ingested_is_reported_as_missing_not_wrong(self, db_session):
        first = event(EventType.POD_FAILED, EventSource.KUBERNETES)
        ingest_event(db_session, first)
        result = correlation_accuracy(db_session, [[first.event_id, "evt_never_seen"]])
        assert result.groups[0].correct is False
        assert result.groups[0].missing_event_ids == ("evt_never_seen",)

    def test_no_expected_groups_is_not_measured_rather_than_a_perfect_score(self, db_session):
        result = correlation_accuracy(db_session, [])
        assert result.denominator == 0
        assert result.rate is None
        assert result.groups == []

    def test_a_single_event_group_is_correct_if_ingested(self, db_session):
        first = event(EventType.POD_FAILED, EventSource.KUBERNETES)
        ingest_event(db_session, first)
        result = correlation_accuracy(db_session, [[first.event_id]])
        assert result.rate == 1.0


class TestDuplicateHandlingRate:
    def test_a_replayed_event_is_recorded_exactly_once(self, db_session):
        original = event(EventType.POD_FAILED, EventSource.KUBERNETES)
        ingest_event(db_session, original)
        ingest_event(db_session, original)  # replay
        result = duplicate_handling_rate(db_session, [original.event_id])
        assert result.rate == 1.0
        assert result.correctly_deduplicated == 1
        assert result.duplicated == 0
        assert result.missing == 0
        assert result.denominator == 1

    def test_an_id_never_ingested_is_missing_not_a_failure(self, db_session):
        result = duplicate_handling_rate(db_session, ["evt_never_seen"])
        assert result.missing == 1
        assert result.correctly_deduplicated == 0
        assert result.denominator == 0
        assert result.rate is None

    def test_empty_input_is_not_measured(self, db_session):
        result = duplicate_handling_rate(db_session, [])
        assert result.requested == 0
        assert result.denominator == 0
        assert result.rate is None

    def test_repeated_ids_in_the_input_are_only_counted_once(self, db_session):
        original = event(EventType.POD_FAILED, EventSource.KUBERNETES)
        ingest_event(db_session, original)
        result = duplicate_handling_rate(db_session, [original.event_id, original.event_id])
        assert result.requested == 1
        assert result.denominator == 1

    def test_the_delivery_log_reports_how_much_redelivery_occurred(self, db_session):
        """One row per event id is guaranteed by the unique index, so the
        rate alone is 1.00 by construction. `redeliveries` is what says
        whether anything was actually redelivered for it to mean
        something."""
        original = event(EventType.POD_FAILED, EventSource.KUBERNETES)
        ingest_event(db_session, original)
        result = duplicate_handling_rate(
            db_session, [original.event_id, original.event_id, original.event_id]
        )
        assert result.deliveries == 3
        assert result.redeliveries == 2
        assert result.rate == 1.0

    def test_a_rate_of_one_over_no_redeliveries_is_reported_as_such(self, db_session):
        """The tautology guard: a caller that deduplicates before calling
        gets rate 1.00 having proven nothing, and redeliveries==0 is the
        only thing that says so."""
        original = event(EventType.POD_FAILED, EventSource.KUBERNETES)
        ingest_event(db_session, original)
        result = duplicate_handling_rate(db_session, [original.event_id])
        assert result.rate == 1.0
        assert result.redeliveries == 0

    def test_a_second_insert_for_the_same_event_id_is_rejected_by_the_schema(self, db_session):
        """`event_records.event_id` carries a unique index (state/models.py)
        specifically so a duplicate row cannot exist for the metric to
        find. This does not test `duplicate_handling_rate` directly - it
        documents *why* `duplicated` is expected to stay at zero against
        this schema, which is what the module docstring claims."""
        import pytest
        from sqlalchemy.exc import IntegrityError

        incident = bare_incident(db_session)

        def add_row():
            db_session.add(
                models.EventRecord(
                    incident_id=incident.id,
                    event_id="evt_dupe",
                    event_type=EventType.POD_FAILED.value,
                    source=EventSource.KUBERNETES.value,
                    service="payment-api",
                    environment="sandbox",
                    occurred_at=T0,
                    envelope={},
                )
            )
            db_session.flush()

        add_row()
        with pytest.raises(IntegrityError):
            add_row()
        db_session.rollback()


class TestEventLatency:
    def test_latency_is_created_at_minus_occurred_at(self, db_session):
        incident = bare_incident(db_session)
        db_session.add(
            models.EventRecord(
                incident_id=incident.id,
                event_id="evt_a",
                event_type=EventType.POD_FAILED.value,
                source=EventSource.KUBERNETES.value,
                service="payment-api",
                environment="sandbox",
                occurred_at=T0,
                created_at=T0 + timedelta(seconds=10),
                envelope={},
            )
        )
        db_session.flush()
        result = event_latency(db_session)
        assert result.count == 1
        assert result.median_seconds == 10.0
        assert result.mean_seconds == 10.0
        assert result.min_seconds == 10.0
        assert result.max_seconds == 10.0
        assert result.p95_seconds == 10.0

    def test_helix_lifecycle_events_are_excluded_but_counted(self, db_session):
        """Stamped at the moment Helix writes them - including them would
        drag the distribution toward a near-zero, flattering number."""
        incident = bare_incident(db_session)
        transition(incident, IncidentStatus.DIAGNOSING, cause="x", session=db_session)
        db_session.flush()
        result = event_latency(db_session)
        assert result.count == 0
        assert result.excluded_helix_count == 0  # record_creation was never called on bare_incident
        # Drive a real creation event to exercise the exclusion itself.
        incident2 = open_incident(db_session)
        db_session.flush()
        result2 = event_latency(db_session)
        assert result2.count == 0
        assert result2.excluded_helix_count == 1
        assert incident2.incident_id  # sanity: the helix-only incident exists

    def test_empty_database_has_no_latency_and_no_division_by_zero(self, db_session):
        result = event_latency(db_session)
        assert result.count == 0
        assert result.median_seconds is None
        assert result.mean_seconds is None
        assert result.p95_seconds is None
        assert result.min_seconds is None
        assert result.max_seconds is None
        assert result.excluded_helix_count == 0

    def test_multiple_events_compute_median_and_p95_over_the_sample(self, db_session):
        incident = bare_incident(db_session)
        offsets = [1, 2, 3, 4, 100]
        for i, offset in enumerate(offsets):
            db_session.add(
                models.EventRecord(
                    incident_id=incident.id,
                    event_id=f"evt_{i}",
                    event_type=EventType.POD_FAILED.value,
                    source=EventSource.KUBERNETES.value,
                    service="payment-api",
                    environment="sandbox",
                    occurred_at=T0,
                    created_at=T0 + timedelta(seconds=offset),
                    envelope={},
                )
            )
        db_session.flush()
        result = event_latency(db_session)
        assert result.count == 5
        assert result.median_seconds == 3.0
        assert result.min_seconds == 1.0
        assert result.max_seconds == 100.0
        assert result.p95_seconds >= result.median_seconds


class TestReplayCorrectness:
    def test_a_fully_driven_incident_replays_correctly(self, db_session):
        incident = open_incident(db_session)
        transition(incident, IncidentStatus.DIAGNOSING, cause="d", session=db_session)
        transition(incident, IncidentStatus.REMEDIATION_PENDING, cause="plan", session=db_session)
        db_session.flush()
        result = replay_correctness(db_session)
        assert result.correct == 1
        assert result.incorrect == 0
        assert result.not_replayable == 0
        assert result.rate == 1.0
        assert result.denominator == 1
        assert result.total_incidents == 1

    def test_an_incident_with_no_lifecycle_events_is_not_replayable_not_a_failure(self, db_session):
        bare_incident(db_session)
        result = replay_correctness(db_session)
        assert result.not_replayable == 1
        assert result.correct == 0
        assert result.incorrect == 0
        assert result.denominator == 0
        assert result.rate is None
        assert result.total_incidents == 1

    def test_empty_database_is_not_measured(self, db_session):
        result = replay_correctness(db_session)
        assert result.total_incidents == 0
        assert result.denominator == 0
        assert result.rate is None
        assert result.mismatches == []

    def test_scoping_to_specific_incident_ids_excludes_the_rest(self, db_session):
        open_incident(db_session)
        bare = bare_incident(db_session)
        result = replay_correctness(db_session, incident_ids=[bare.incident_id])
        assert result.total_incidents == 1
        assert result.not_replayable == 1


class TestEventLayerReport:
    def test_report_shape_is_json_serializable(self, db_session):
        import json

        chain = four_source_chain()
        for e in chain:
            ingest_event(db_session, e)
        report = event_layer_report(db_session, expected_groups=[[e.event_id for e in chain]])
        json.dumps(report)  # raises if anything is not serializable
        assert set(report) == {
            "correlation_accuracy",
            "duplicate_handling_rate",
            "event_latency",
            "replay_correctness",
        }
        assert report["correlation_accuracy"]["rate"] == 1.0
        assert report["duplicate_handling_rate"]["rate"] == 1.0

    def test_report_without_expected_groups_marks_correlation_unmeasured(self, db_session):
        report = event_layer_report(db_session)
        assert report["correlation_accuracy"]["rate"] is None
        assert report["correlation_accuracy"]["denominator"] == 0
        assert "note" in report["correlation_accuracy"]

    def test_report_on_an_empty_database_has_no_rates_and_no_crash(self, db_session):
        report = event_layer_report(db_session)
        assert report["duplicate_handling_rate"]["rate"] is None
        assert report["event_latency"]["count"] == 0
        assert report["replay_correctness"]["rate"] is None


class TestRenderReportText:
    def test_a_not_measured_correlation_note_is_included_verbatim(self):
        report = {
            "correlation_accuracy": {
                "rate": None,
                "correct": 0,
                "denominator": 0,
                "note": "not measured: no expected_groups (ground truth) provided",
            },
            "duplicate_handling_rate": {
                "rate": 1.0,
                "correctly_deduplicated": 1,
                "duplicated": 0,
                "missing": 0,
                "denominator": 1,
                "deliveries": 1,
                "redeliveries": 0,
            },
            "event_latency": {
                "count": 0,
                "median_seconds": None,
                "mean_seconds": None,
                "p95_seconds": None,
                "min_seconds": None,
                "max_seconds": None,
                "excluded_helix_count": 0,
            },
            "replay_correctness": {
                "rate": None,
                "correct": 0,
                "incorrect": 0,
                "not_replayable": 0,
                "denominator": 0,
                "total_incidents": 0,
            },
        }
        text = render_report_text(report)
        assert "not measured: no expected_groups (ground truth) provided" in text
        assert "denominator=1" in text
        assert "excluded_helix_count=0" in text


class TestMainCLI:
    """`scripts/event-metrics.sh` -> `python -m events.metrics`. Exercised
    against a real (file-backed, not in-memory) SQLite database rather than
    a mock, since the point of this CLI is that it talks to a real engine
    built from `--database-url`."""

    def _empty_db_url(self, tmp_path, name: str) -> str:
        url = f"sqlite:///{tmp_path / name}"
        engine = create_engine(url)
        models.Base.metadata.create_all(engine)
        engine.dispose()
        return url

    def test_main_prints_a_human_readable_report_by_default(self, tmp_path, capsys):
        url = self._empty_db_url(tmp_path, "cli-text.db")
        exit_code = main(["--database-url", url])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Event layer report (subphase E7)" in out
        assert "correlation_accuracy: rate=None" in out
        assert "not measured: no expected_groups" in out

    def test_main_json_flag_emits_the_same_shape_as_event_layer_report(self, tmp_path, capsys):
        url = self._empty_db_url(tmp_path, "cli-json.db")
        exit_code = main(["--database-url", url, "--json"])
        out = capsys.readouterr().out
        assert exit_code == 0
        payload = json.loads(out)
        assert set(payload) == {
            "correlation_accuracy",
            "duplicate_handling_rate",
            "event_latency",
            "replay_correctness",
        }
        assert payload["correlation_accuracy"]["rate"] is None
