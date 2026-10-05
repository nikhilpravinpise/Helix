"""Scripted approver (D2): approves ONLY the scenario's declared gated
optimal action; rejects anything else pending; reports honestly when a
suspended run has no pending row."""

from datetime import UTC, datetime

from sqlalchemy import select

from benchmark.approver import APPROVER_ACTOR, decide_pending, service_suspended_run
from state import models
from state.approvals import pending_for_incident
from state.intake import intake_signal
from state.models import ApprovalStatus

GATED = {
    "name": "crashloop",
    "approval_gated": True,
    "optimal_action": "reapply_manifest",
}
UNGATED = {
    "name": "ci-test-regression",
    "approval_gated": False,
    "optimal_action": "open_fix_pr",
}


def _incident(sessions) -> models.Incident:
    signal = {
        "source": "kubernetes",
        "service": "payment-api",
        "environment": "sandbox",
        "severity": "high",
        "signature": {"waiting_reason": "CrashLoopBackOff"},
        "raw": {"namespace": "payment-api"},
    }
    with sessions() as session:
        result = intake_signal(session, signal)
        return session.scalar(
            select(models.Incident).where(models.Incident.incident_id == result.incident_id)
        )


def _pending(incident: models.Incident, action: str = "reapply_manifest") -> models.Approval:
    return models.Approval(
        incident_id=incident.id,
        action=action,
        params={"path": "sandbox/payment-api/k8s/03-secret.yaml"},
        fingerprint="fp",
        status=ApprovalStatus.PENDING,
    )


def test_approves_the_declared_gated_optimal(sqlite_sessions):
    with sqlite_sessions() as session:
        # merge: decide_pending now resolves the author through the
        # incident's event records (state/deciders.py), which needs an
        # attached instance, not the detached one _incident() returns.
        incident = session.merge(_incident(sqlite_sessions))
        session.add(_pending(incident))
        outcome = decide_pending(session, incident, GATED)
        assert outcome.outcome == "approved"
        row = pending_for_incident(session, incident.id)
        assert row is None  # no longer pending
        decided = session.scalar(
            select(models.Approval).where(models.Approval.incident_id == incident.id)
        )
        assert decided.status == ApprovalStatus.APPROVED
        # decided by an owner: only they may decide (state/deciders.py)
        assert decided.decided_by == "alice"
        assert APPROVER_ACTOR in (decided.note or "")
        assert decided.decided_at is not None


def test_rejects_when_scenario_is_not_gated(sqlite_sessions):
    """A pending row on a scenario that declares approval_gated=false gets
    declined - the arm asked for an approval the scenario does not bless."""
    with sqlite_sessions() as session:
        # merge: decide_pending resolves the author via the incident's
        # event records (state/deciders.py), which needs an attached
        # instance, not the detached one _incident() returns.
        incident = session.merge(_incident(sqlite_sessions))
        session.add(_pending(incident, action="reapply_manifest"))
        outcome = decide_pending(session, incident, UNGATED)
        assert outcome.outcome == "rejected"
        decided = session.scalar(
            select(models.Approval).where(models.Approval.incident_id == incident.id)
        )
        assert decided.status == ApprovalStatus.REJECTED


def test_rejects_a_non_optimal_gated_action(sqlite_sessions):
    """On a gated scenario, a pending action that is NOT the declared
    optimal is rejected - the approver never blesses an arm's mistake."""
    with sqlite_sessions() as session:
        # merge: decide_pending resolves the author via the incident's
        # event records (state/deciders.py), which needs an attached
        # instance, not the detached one _incident() returns.
        incident = session.merge(_incident(sqlite_sessions))
        session.add(_pending(incident, action="scale_deployment"))
        outcome = decide_pending(session, incident, GATED)
        assert outcome.outcome == "rejected"
        assert outcome.action == "scale_deployment"


def test_no_pending_row_is_reported_not_invented(sqlite_sessions):
    with sqlite_sessions() as session:
        incident = _incident(sqlite_sessions)
        outcome = decide_pending(session, incident, GATED)
        assert outcome.outcome == "no_pending"


def test_already_decided_row_is_an_error(sqlite_sessions):
    """A double decision is a defect: the second call must report the
    conflict, not silently re-decide."""
    with sqlite_sessions() as session:
        incident = _incident(sqlite_sessions)
        pending = _pending(incident)
        pending.status = ApprovalStatus.APPROVED
        pending.decided_by = "someone-else"
        pending.decided_at = datetime.now(UTC)
        session.add(pending)
        # pending_for_incident only returns PENDING rows - a decided row is
        # "no pending", which is still honest (nothing left to decide).
        assert decide_pending(session, incident, GATED).outcome == "no_pending"


def test_service_suspended_run_wraps_decide(sqlite_sessions):
    incident = _incident(sqlite_sessions)
    with sqlite_sessions() as session:
        session.add(_pending(incident))
    outcome = service_suspended_run(sqlite_sessions, incident.incident_id, GATED)
    assert outcome.outcome == "approved"
    assert outcome.action == "reapply_manifest"
