"""Unit tests for the change-review lifecycle's `_decide` branches
(subphase 2.1/2.5/2.6) - the coordinator's pure decision function, tested
the same way `tests/agent/test_policy.py` tests `agent.policy.decide`:
no graph, no session, just (state, approval, triage, review, ...) ->
_Decision. Every hop is its own decision, exactly like the rest of the
Phase 1 machine - a caller (test, benchmark harness, CLI) advances the
incident by calling `run_incident`/`_decide` again, never by assuming one
call jumps multiple statuses.
"""

from agent.agents.coordinator import _decide
from state.machine import IncidentStatus
from state.models import ApprovalStatus


class _Row:
    """A tiny stand-in for a SQLAlchemy row - `_decide` only reads
    attributes off `triage`/`review`, never queries with them."""


def _triage_row(*, owner=None, owner_source="registry", duplicate_of_id=None, rationale=""):
    row = _Row()
    row.owner = owner
    row.owner_source = owner_source
    row.duplicate_of_id = duplicate_of_id
    row.rationale = rationale
    return row


def _review_row(status: ApprovalStatus, *, decided_by: str = "bob", note: str = ""):
    row = _Row()
    row.status = status
    row.decided_by = decided_by
    row.note = note
    return row


def test_received_change_without_triage_parks() -> None:
    decision = _decide({"status": IncidentStatus.RECEIVED, "kind": "change"}, None, triage=None)
    assert decision.to_status is None


def test_received_recovery_still_goes_to_diagnosing_unchanged() -> None:
    """Phase 1 behavior must survive Phase 2 additions bit for bit."""
    decision = _decide({"status": IncidentStatus.RECEIVED, "kind": "recovery"}, None)
    assert decision.to_status == IncidentStatus.DIAGNOSING


def test_received_recovery_ignores_a_triage_row_if_somehow_present() -> None:
    """kind is authoritative - a stray triage row never diverts a
    recovery incident."""
    decision = _decide(
        {"status": IncidentStatus.RECEIVED, "kind": "recovery"},
        None,
        triage=_triage_row(owner="alice"),
    )
    assert decision.to_status == IncidentStatus.DIAGNOSING


def test_received_change_missing_kind_defaults_to_recovery_routing() -> None:
    """A state dict with no `kind` key at all (a caller that predates 2.1)
    must behave exactly like recovery - never silently divert to the
    change lifecycle."""
    decision = _decide({"status": IncidentStatus.RECEIVED}, None)
    assert decision.to_status == IncidentStatus.DIAGNOSING


def test_received_change_triaged_transitions_to_triaged() -> None:
    state = {"status": IncidentStatus.RECEIVED, "kind": "change"}
    decision = _decide(state, None, triage=_triage_row(owner="alice", rationale="owner alice"))
    assert decision.to_status == IncidentStatus.TRIAGED


def test_received_change_triaged_duplicate_escalates() -> None:
    state = {"status": IncidentStatus.RECEIVED, "kind": "change"}
    decision = _decide(
        state,
        None,
        triage=_triage_row(owner="alice", duplicate_of_id=7, rationale="duplicate of INC-orig"),
    )
    assert decision.to_status == IncidentStatus.ESCALATED
    assert decision.duplicate_of_id == 7


def test_received_change_triaged_no_owner_escalates() -> None:
    state = {"status": IncidentStatus.RECEIVED, "kind": "change"}
    decision = _decide(state, None, triage=_triage_row(owner=None, rationale="no owner resolved"))
    assert decision.to_status == IncidentStatus.ESCALATED


def test_triaged_without_row_escalates() -> None:
    decision = _decide({"status": IncidentStatus.TRIAGED}, None, triage=None)
    assert decision.to_status == IncidentStatus.ESCALATED


def test_triaged_with_owner_assigns_it() -> None:
    decision = _decide(
        {"status": IncidentStatus.TRIAGED},
        None,
        triage=_triage_row(owner="alice", owner_source="registry"),
    )
    assert decision.to_status == IncidentStatus.OWNER_ASSIGNED
    assert decision.set_owner == "alice"


def test_owner_assigned_without_review_parks() -> None:
    decision = _decide({"status": IncidentStatus.OWNER_ASSIGNED, "review_complete": False}, None)
    assert decision.to_status is None


def test_owner_assigned_with_reviewer_requests_review() -> None:
    state = {
        "status": IncidentStatus.OWNER_ASSIGNED,
        "review_complete": True,
        "review": {"reviewer": "bob", "rationale": "next available"},
    }
    decision = _decide(state, None)
    assert decision.to_status == IncidentStatus.REVIEW_PENDING


def test_owner_assigned_no_reviewer_escalates() -> None:
    state = {
        "status": IncidentStatus.OWNER_ASSIGNED,
        "review_complete": True,
        "review": {"reviewer": None, "rationale": "every candidate excluded"},
    }
    decision = _decide(state, None)
    assert decision.to_status == IncidentStatus.ESCALATED


def test_review_pending_no_row_parks() -> None:
    decision = _decide({"status": IncidentStatus.REVIEW_PENDING}, None, review=None)
    assert decision.to_status is None


def test_review_pending_still_pending_parks() -> None:
    row = _review_row(ApprovalStatus.PENDING)
    decision = _decide({"status": IncidentStatus.REVIEW_PENDING}, None, review=row)
    assert decision.to_status is None


def test_review_pending_approved_advances() -> None:
    row = _review_row(ApprovalStatus.APPROVED)
    decision = _decide({"status": IncidentStatus.REVIEW_PENDING}, None, review=row)
    assert decision.to_status == IncidentStatus.APPROVED


def test_review_pending_rejected_advances_to_rejected() -> None:
    row = _review_row(ApprovalStatus.REJECTED, note="needs a config fix")
    decision = _decide({"status": IncidentStatus.REVIEW_PENDING}, None, review=row)
    assert decision.to_status == IncidentStatus.REJECTED


def test_rejected_advances_to_fix_required() -> None:
    decision = _decide({"status": IncidentStatus.REJECTED}, None)
    assert decision.to_status == IncidentStatus.FIX_REQUIRED


def test_fix_required_without_fix_pushed_parks() -> None:
    decision = _decide({"status": IncidentStatus.FIX_REQUIRED}, None, fix_pushed=False)
    assert decision.to_status is None


def test_fix_required_fix_pushed_without_review_complete_parks() -> None:
    decision = _decide(
        {"status": IncidentStatus.FIX_REQUIRED, "review_complete": False},
        None,
        fix_pushed=True,
    )
    assert decision.to_status is None


def test_fix_required_fix_pushed_re_requests_review() -> None:
    state = {
        "status": IncidentStatus.FIX_REQUIRED,
        "review_complete": True,
        "review": {"reviewer": "carol", "rationale": "next available"},
    }
    decision = _decide(state, None, fix_pushed=True)
    assert decision.to_status == IncidentStatus.REVIEW_PENDING


def test_fix_required_fix_pushed_no_reviewer_escalates() -> None:
    state = {
        "status": IncidentStatus.FIX_REQUIRED,
        "review_complete": True,
        "review": {"reviewer": None, "rationale": "every candidate excluded"},
    }
    decision = _decide(state, None, fix_pushed=True)
    assert decision.to_status == IncidentStatus.ESCALATED


def test_approved_advances_to_deploying() -> None:
    decision = _decide({"status": IncidentStatus.APPROVED}, None)
    assert decision.to_status == IncidentStatus.DEPLOYING


def test_deploying_no_outcome_parks() -> None:
    decision = _decide({"status": IncidentStatus.DEPLOYING}, None, deploy_outcome=None)
    assert decision.to_status is None


def test_deploying_passed_recovers() -> None:
    decision = _decide({"status": IncidentStatus.DEPLOYING}, None, deploy_outcome="passed")
    assert decision.to_status == IncidentStatus.RECOVERED


def test_deploying_failed_hands_off_to_diagnosing() -> None:
    decision = _decide({"status": IncidentStatus.DEPLOYING}, None, deploy_outcome="failed")
    assert decision.to_status == IncidentStatus.DIAGNOSING
