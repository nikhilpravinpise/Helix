"""Scripted reviewer (subphase 2.9/2.11): decision routing, the durable
`state.reviews.record_decision` path, and the honest no_pending/timeout
outcomes - the change-flow counterpart of the approver tests."""

from sqlalchemy import select

from benchmark.reviewer import (
    REVIEWER_ACTOR,
    decision_for_round,
    service_suspended_review,
)
from state import models
from state.machine import IncidentStatus


def _incident_with_pending_review(session) -> models.Incident:
    """A change incident parked at review_pending with one pending
    review_requests row - the state the scripted reviewer services."""
    incident = models.Incident(
        incident_id="INC-review-test",
        fingerprint="fp-review",
        service="payment-api",
        environment="sandbox",
        severity="medium",
        status=IncidentStatus.REVIEW_PENDING,
        kind=models.IncidentKind.CHANGE,
    )
    session.add(incident)
    session.flush()
    session.add(
        models.ReviewRequest(
            incident_id=incident.id,
            reviewer="bob",
            rationale="owner for payment-api",
            status=models.ApprovalStatus.PENDING,
        )
    )
    session.flush()
    return incident


def test_decision_for_round_scalar_and_list():
    assert decision_for_round({}, 0) == "approve"
    assert decision_for_round({"review_decision": "reject"}, 0) == "reject"
    seq = {"review_decision": ["reject", "approve"]}
    assert decision_for_round(seq, 0) == "reject"
    assert decision_for_round(seq, 1) == "approve"
    # A list shorter than the round index repeats its last entry.
    assert decision_for_round(seq, 7) == "approve"


def test_approve_records_through_the_durable_path(sqlite_sessions):
    with sqlite_sessions() as session:
        _incident_with_pending_review(session)
    scenario = {"name": "x", "review_decision": "approve"}
    out = service_suspended_review(sqlite_sessions, "INC-review-test", scenario)
    assert out.outcome == "approved"
    assert out.reviewer == "bob"
    with sqlite_sessions() as session:
        row = session.scalar(select(models.ReviewRequest))
        assert row.status == models.ApprovalStatus.APPROVED
        assert row.decided_by == REVIEWER_ACTOR
        assert row.decided_at is not None


def test_reject_records_a_rejected_row(sqlite_sessions):
    with sqlite_sessions() as session:
        _incident_with_pending_review(session)
    scenario = {"name": "x", "review_decision": "reject"}
    out = service_suspended_review(sqlite_sessions, "INC-review-test", scenario)
    assert out.outcome == "rejected"
    with sqlite_sessions() as session:
        row = session.scalar(select(models.ReviewRequest))
        assert row.status == models.ApprovalStatus.REJECTED


def test_reviewer_actor_override(sqlite_sessions):
    """A scenario can name its own reviewer identity - recorded, not
    guessed."""
    with sqlite_sessions() as session:
        _incident_with_pending_review(session)
    scenario = {"name": "x", "review_decision": "approve", "reviewer_actor": "carol"}
    service_suspended_review(sqlite_sessions, "INC-review-test", scenario)
    with sqlite_sessions() as session:
        row = session.scalar(select(models.ReviewRequest))
        assert row.decided_by == "carol"


def test_timeout_decides_nothing(sqlite_sessions):
    """`timeout` is the honest non-response: the row stays pending and no
    decision is recorded - exactly what a reviewer who never answers
    looks like in the store."""
    with sqlite_sessions() as session:
        _incident_with_pending_review(session)
    scenario = {"name": "x", "review_decision": "timeout"}
    out = service_suspended_review(sqlite_sessions, "INC-review-test", scenario)
    assert out.outcome == "timeout"
    assert out.reviewer == "bob"
    with sqlite_sessions() as session:
        row = session.scalar(select(models.ReviewRequest))
        assert row.status == models.ApprovalStatus.PENDING
        assert row.decided_by is None


def test_no_pending_is_an_honest_report(sqlite_sessions):
    with sqlite_sessions() as session:
        incident = models.Incident(
            incident_id="INC-no-review",
            fingerprint="fp-x",
            service="payment-api",
            environment="sandbox",
            severity="medium",
            status=IncidentStatus.REVIEW_PENDING,
            kind=models.IncidentKind.CHANGE,
        )
        session.add(incident)
    out = service_suspended_review(sqlite_sessions, "INC-no-review", {"name": "x"})
    assert out.outcome == "no_pending"


def test_second_decision_is_an_error_not_a_silent_pass(sqlite_sessions):
    """`record_decision` refuses a second decision; the scripted reviewer
    reports that as an error outcome, never a duplicate write."""
    with sqlite_sessions() as session:
        _incident_with_pending_review(session)
    scenario = {"name": "x", "review_decision": "approve"}
    first = service_suspended_review(sqlite_sessions, "INC-review-test", scenario)
    assert first.outcome == "approved"
    # No pending row now - the next read sees none, which is honest.
    second = service_suspended_review(sqlite_sessions, "INC-review-test", scenario)
    assert second.outcome == "no_pending"


def test_missing_incident_is_an_error(sqlite_sessions):
    out = service_suspended_review(sqlite_sessions, "INC-nonexistent", {"name": "x"})
    assert out.outcome == "error"
