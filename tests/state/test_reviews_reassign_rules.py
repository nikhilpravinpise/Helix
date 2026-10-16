"""Reassigning a pending review (decided 2026-10-16): owners and admins hand it
to anyone on the team except the change's author. Who may decide is unchanged,
so after the hand-over the new reviewer decides it and the old one is refused."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from events.ingest import ingest_event
from events.schemas import EngineeringEvent, EventSource, EventType
from state import models
from state.reviews import ReviewDecidedError, ReviewReassignError, reassign
from state.reviews import record_decision as decide_review

BENCHMARK_REGISTRY = Path(__file__).resolve().parents[2] / "benchmark" / "service_registry.yaml"


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    """alice owner, bob admin, carol and dave members; ivy is only invited."""
    path = tmp_path / "registry.yaml"
    path.write_text(
        BENCHMARK_REGISTRY.read_text() + "\npeople:\n  alice: {name: Alice Anders, role: owner}\n"
        "  bob: {name: Bob Brown, role: admin}\n  carol: {role: member}\n  dave: {role: member}\n"
    )
    monkeypatch.setenv("HELIX_SERVICE_REGISTRY", str(path))


def _review(db_session, *, author="alice", reviewer="carol") -> models.ReviewRequest:
    result = ingest_event(
        db_session,
        EngineeringEvent(
            event_type=EventType.PR_OPENED,
            source=EventSource.GITHUB,
            service="payment-api",
            environment="sandbox",
            actor=author,
            metadata={"pr": 9, "branch": "feature/x"},
        ),
    )
    incident = db_session.query(models.Incident).filter_by(incident_id=result.incident_id).one()
    row = models.ReviewRequest(
        incident_id=incident.id,
        attempt=0,
        reviewer=reviewer,
        rationale="registry pick",
        status=models.ApprovalStatus.PENDING,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _outbox(db_session, review) -> list[models.IntegrationOutbox]:
    return list(
        db_session.scalars(
            select(models.IntegrationOutbox)
            .where(models.IntegrationOutbox.op == "review_request")
            .where(models.IntegrationOutbox.incident_id == review.incident_id)
        )
    )


class TestReassign:
    @pytest.mark.parametrize("role", ["owner", "admin", None])
    def test_owner_admin_and_sign_in_off_may_reassign(self, db_session, role):
        review = _review(db_session)
        reassign(db_session, review, "dave", "bob", actor_role=role)
        assert review.reviewer == "dave"
        assert review.status == models.ApprovalStatus.PENDING

    def test_a_member_may_not(self, db_session):
        review = _review(db_session)
        with pytest.raises(ReviewReassignError, match="owner or admin"):
            reassign(db_session, review, "dave", "carol", actor_role="member")
        assert review.reviewer == "carol"

    @pytest.mark.parametrize(
        "status", [models.ApprovalStatus.APPROVED, models.ApprovalStatus.REJECTED]
    )
    def test_only_a_pending_review(self, db_session, status):
        review = _review(db_session)
        review.status = status
        with pytest.raises(ReviewReassignError, match="already"):
            reassign(db_session, review, "dave", "bob", actor_role="admin")
        assert review.reviewer == "carol"

    def test_the_new_reviewer_must_be_on_the_team(self, db_session):
        review = _review(db_session)
        with pytest.raises(ReviewReassignError, match="not on the team"):
            reassign(db_session, review, "mallory", "bob", actor_role="admin")
        assert review.reviewer == "carol"

    def test_a_pending_invitee_does_not_count(self, db_session):
        db_session.add(
            models.Invitation(
                login="ivy",
                role="member",
                invited_by="alice",
                token="t",
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        db_session.flush()
        review = _review(db_session)
        with pytest.raises(ReviewReassignError, match="not on the team"):
            reassign(db_session, review, "ivy", "bob", actor_role="admin")

    def test_the_author_is_refused_even_as_owner(self, db_session):
        review = _review(db_session, author="alice")
        with pytest.raises(ReviewReassignError, match="wrote this change"):
            reassign(db_session, review, "alice", "alice", actor_role="owner")
        assert review.reviewer == "carol"

    def test_the_current_reviewer_is_refused(self, db_session):
        review = _review(db_session)
        with pytest.raises(ReviewReassignError, match="already the reviewer"):
            reassign(db_session, review, "carol", "bob", actor_role="admin")

    def test_the_rationale_records_who_and_from_whom(self, db_session):
        review = _review(db_session)
        reassign(db_session, review, "dave", "bob", actor_role="admin")
        assert review.rationale == "registry pick\nReassigned by bob (was carol)."
        reassign(db_session, review, "carol", "alice", actor_role="owner")
        assert review.rationale.endswith("\nReassigned by alice (was dave).")
        assert "Reassigned by bob (was carol)." in review.rationale

    def test_the_new_reviewer_is_pinged_through_the_review_request(self, db_session):
        review = _review(db_session)
        reassign(db_session, review, "dave", "bob", actor_role="admin")
        (row,) = _outbox(db_session, review)
        assert row.integration == "slack"
        assert row.payload["review_id"] == review.id
        assert "dave" in row.payload["text"] and "Reassigned by bob" in row.payload["text"]

    def test_a_refused_reassignment_pings_nobody(self, db_session):
        review = _review(db_session)
        with pytest.raises(ReviewReassignError):
            reassign(db_session, review, "mallory", "bob", actor_role="admin")
        assert _outbox(db_session, review) == []


class TestDecisionRulesAreUnchanged:
    def test_the_old_reviewer_is_refused_and_the_new_one_decides(self, db_session):
        review = _review(db_session)
        reassign(db_session, review, "dave", "bob", actor_role="admin")
        with pytest.raises(ReviewDecidedError, match="not allowed"):
            decide_review(db_session, review, True, "carol", timeout_s=0)
        decide_review(db_session, review, True, "dave", timeout_s=0)
        assert review.status == models.ApprovalStatus.APPROVED
        assert review.decided_by == "dave"
