"""Who may decide an approval or a review (Phase 2 human-in-the-loop rules).

Audit 2026-10-05 (I4): any Slack user who saw a card, or any `--actor` on
the CLI and API, could decide a review or an approval - including a PR's
author approving their own change. The rules now enforced in
`record_decision`, the one path every surface uses:

- an approval is decided only by an owner of the incident's service;
- a review is decided only by the reviewer it was requested from;
- the author of the change decides neither.

The benchmark registry (tests/conftest.py) names payment-api's owners
alice and bob, and reviewers bob and carol.
"""

import pytest

from events.ingest import ingest_event
from events.schemas import EngineeringEvent, EventSource, EventType
from state import models
from state.approvals import ApprovalDecidedError
from state.approvals import record_decision as decide_approval
from state.machine import IncidentStatus
from state.reviews import ReviewDecidedError
from state.reviews import record_decision as decide_review


def _incident(db_session, *, author="dana", service="payment-api") -> models.Incident:
    result = ingest_event(
        db_session,
        EngineeringEvent(
            event_type=EventType.PR_OPENED,
            source=EventSource.GITHUB,
            service=service,
            environment="sandbox",
            actor=author,
            metadata={"pr": 9, "branch": "feature/x"},
        ),
    )
    return db_session.query(models.Incident).filter_by(incident_id=result.incident_id).one()


def _approval(db_session, incident) -> models.Approval:
    row = models.Approval(
        incident_id=incident.id,
        action="restart_deployment",
        params={"deployment": "payment-api", "namespace": "payment-api"},
        fingerprint="f" * 64,
        status=models.ApprovalStatus.PENDING,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _review(db_session, incident, reviewer="bob") -> models.ReviewRequest:
    row = models.ReviewRequest(
        incident_id=incident.id,
        attempt=0,
        reviewer=reviewer,
        rationale="registry",
        status=models.ApprovalStatus.PENDING,
    )
    db_session.add(row)
    db_session.flush()
    return row


class TestApprovals:
    def test_an_owner_may_approve(self, db_session):
        approval = _approval(db_session, _incident(db_session))
        decide_approval(db_session, approval, True, "alice", timeout_s=0)
        assert approval.status == models.ApprovalStatus.APPROVED

    def test_an_owner_may_reject(self, db_session):
        approval = _approval(db_session, _incident(db_session))
        decide_approval(db_session, approval, False, "bob", timeout_s=0)
        assert approval.status == models.ApprovalStatus.REJECTED

    @pytest.mark.parametrize("actor", ["carol", "operator", "slack", "mallory"])
    def test_anyone_else_is_refused(self, db_session, actor):
        approval = _approval(db_session, _incident(db_session))
        with pytest.raises(ApprovalDecidedError, match="not allowed"):
            decide_approval(db_session, approval, True, actor, timeout_s=0)
        assert approval.status == models.ApprovalStatus.PENDING
        assert approval.decided_by is None

    def test_the_author_of_the_change_is_refused_even_as_owner(self, db_session):
        approval = _approval(db_session, _incident(db_session, author="alice"))
        with pytest.raises(ApprovalDecidedError, match="author"):
            decide_approval(db_session, approval, True, "alice", timeout_s=0)
        decide_approval(db_session, approval, True, "bob", timeout_s=0)
        assert approval.decided_by == "bob"

    def test_a_service_with_no_owners_cannot_be_approved(self, db_session):
        approval = _approval(db_session, _incident(db_session, service="billing-worker"))
        with pytest.raises(ApprovalDecidedError, match="has no approvers who may") as err:
            decide_approval(db_session, approval, True, "alice", timeout_s=0)
        assert "(add approvers on the Services page)" in str(err.value)


class TestReviews:
    def test_the_requested_reviewer_may_decide(self, db_session):
        review = _review(db_session, _incident(db_session), reviewer="bob")
        decide_review(db_session, review, True, "bob", timeout_s=0)
        assert review.status == models.ApprovalStatus.APPROVED

    @pytest.mark.parametrize("actor", ["carol", "alice", "operator"])
    def test_anyone_else_is_refused(self, db_session, actor):
        review = _review(db_session, _incident(db_session), reviewer="bob")
        with pytest.raises(ReviewDecidedError, match="not allowed"):
            decide_review(db_session, review, True, actor, timeout_s=0)
        assert review.status == models.ApprovalStatus.PENDING

    def test_the_author_never_reviews_their_own_change(self, db_session):
        review = _review(db_session, _incident(db_session, author="bob"), reviewer="bob")
        with pytest.raises(ReviewDecidedError, match="author"):
            decide_review(db_session, review, True, "bob", timeout_s=0)


def test_status_is_unchanged_by_a_refusal(db_session):
    incident = _incident(db_session)
    approval = _approval(db_session, incident)
    with pytest.raises(ApprovalDecidedError):
        decide_approval(db_session, approval, True, "mallory", timeout_s=0)
    assert incident.status == IncidentStatus.RECEIVED


# Self-approval (decided 2026-10-16): owners and admins may approve their OWN
# change, but only by asking explicitly (`self_approve=True`). Everything else
# keeps the strict rule above.
ROLE_REGISTRY = """
services:
  payment-api:
    default_owner: alice
    owners: [bob, carol]
    default_reviewers: [bob, carol]
people:
  alice: {role: owner}
  bob: {role: admin}
  carol: {role: member}
  frank: {role: owner}
"""


@pytest.fixture
def roles(tmp_path, monkeypatch):
    path = tmp_path / "roles.yaml"
    path.write_text(ROLE_REGISTRY)
    monkeypatch.setenv("HELIX_SERVICE_REGISTRY", str(path))


class TestSelfApprovalOfApprovals:
    @pytest.mark.parametrize("author", ["alice", "bob"])
    def test_an_owner_or_admin_author_may_approve_their_own_change(self, db_session, roles, author):
        approval = _approval(db_session, _incident(db_session, author=author))
        decide_approval(db_session, approval, True, author, timeout_s=0, self_approve=True)
        assert approval.status == models.ApprovalStatus.APPROVED
        assert approval.decided_by == author

    @pytest.mark.parametrize("author", ["alice", "bob"])
    def test_without_the_flag_they_are_still_refused(self, db_session, roles, author):
        approval = _approval(db_session, _incident(db_session, author=author))
        with pytest.raises(ApprovalDecidedError, match="author"):
            decide_approval(db_session, approval, True, author, timeout_s=0)
        assert approval.status == models.ApprovalStatus.PENDING

    def test_a_member_author_is_refused_even_with_the_flag(self, db_session, roles):
        approval = _approval(db_session, _incident(db_session, author="carol"))
        with pytest.raises(ApprovalDecidedError, match="author"):
            decide_approval(db_session, approval, True, "carol", timeout_s=0, self_approve=True)
        assert approval.status == models.ApprovalStatus.PENDING

    def test_an_author_not_on_the_team_is_refused_even_with_the_flag(self, db_session, roles):
        approval = _approval(db_session, _incident(db_session, author="ghost"))
        with pytest.raises(ApprovalDecidedError, match="author"):
            decide_approval(db_session, approval, True, "ghost", timeout_s=0, self_approve=True)

    def test_an_owner_author_who_is_not_an_approver_of_the_service_is_refused(
        self, db_session, roles
    ):
        approval = _approval(db_session, _incident(db_session, author="frank"))
        with pytest.raises(ApprovalDecidedError, match="author"):
            decide_approval(db_session, approval, True, "frank", timeout_s=0, self_approve=True)

    def test_the_flag_does_not_let_a_stranger_decide(self, db_session, roles):
        approval = _approval(db_session, _incident(db_session, author="alice"))
        with pytest.raises(ApprovalDecidedError, match="not allowed"):
            decide_approval(db_session, approval, True, "frank", timeout_s=0, self_approve=True)

    def test_the_automatic_pick_still_leaves_the_author_out(self, db_session, roles):
        from state.deciders import approval_deciders

        assert approval_deciders(_incident(db_session, author="bob")) == ["alice", "carol"]

    def test_the_benchmark_registry_has_no_roles_so_the_flag_changes_nothing(self, db_session):
        approval = _approval(db_session, _incident(db_session, author="alice"))
        with pytest.raises(ApprovalDecidedError, match="author"):
            decide_approval(db_session, approval, True, "alice", timeout_s=0, self_approve=True)
        assert approval.status == models.ApprovalStatus.PENDING


class TestSelfApprovalOfReviews:
    @pytest.mark.parametrize("author", ["alice", "bob"])
    def test_an_owner_or_admin_author_may_approve_the_review_requested_from_someone_else(
        self, db_session, roles, author
    ):
        review = _review(db_session, _incident(db_session, author=author), reviewer="carol")
        decide_review(db_session, review, True, author, timeout_s=0, self_approve=True)
        assert review.status == models.ApprovalStatus.APPROVED
        assert review.decided_by == author

    @pytest.mark.parametrize("author", ["alice", "bob"])
    def test_without_the_flag_they_are_still_refused(self, db_session, roles, author):
        review = _review(db_session, _incident(db_session, author=author), reviewer="carol")
        with pytest.raises(ReviewDecidedError, match="author"):
            decide_review(db_session, review, True, author, timeout_s=0)
        assert review.status == models.ApprovalStatus.PENDING

    @pytest.mark.parametrize("author", ["carol", "ghost"])
    def test_members_and_strangers_are_refused_even_with_the_flag(self, db_session, roles, author):
        review = _review(db_session, _incident(db_session, author=author), reviewer="bob")
        with pytest.raises(ReviewDecidedError, match="author"):
            decide_review(db_session, review, True, author, timeout_s=0, self_approve=True)
        assert review.status == models.ApprovalStatus.PENDING

    def test_the_flag_does_not_let_a_non_author_skip_the_reviewer_rule(self, db_session, roles):
        review = _review(db_session, _incident(db_session, author="dana"), reviewer="carol")
        with pytest.raises(ReviewDecidedError, match="not allowed"):
            decide_review(db_session, review, True, "alice", timeout_s=0, self_approve=True)

    def test_the_benchmark_registry_still_refuses_an_author(self, db_session):
        review = _review(db_session, _incident(db_session, author="bob"), reviewer="carol")
        with pytest.raises(ReviewDecidedError, match="author"):
            decide_review(db_session, review, True, "bob", timeout_s=0, self_approve=True)


def test_self_approved_is_a_decision_by_the_author(db_session, roles):
    from state.deciders import is_self_approved

    incident = _incident(db_session, author="alice")
    assert is_self_approved(incident, "alice")
    assert not is_self_approved(incident, "bob")
    assert not is_self_approved(incident, None)
    assert not is_self_approved(_incident(db_session, author="system"), "system")
