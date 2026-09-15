"""IncidentStatus edge-map and transition() tests.

The edge-map tests are pure (no DB). The persistence tests are
integration-marked: they run against the compose Postgres via the `pg`
fixture (conftest applies the real alembic migration).
"""

import pytest
from sqlalchemy import func, select

from state import models
from state.db import session_scope
from state.machine import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    IncidentStatus,
    InvalidTransitionError,
    is_allowed,
    transition,
)

S = IncidentStatus

# The workflow edges from docs/architecture/state-machine.md, minus the
# blanket "any non-terminal -> escalated|failed" rule which has its own
# test below.
WORKFLOW_EDGES = {
    (S.RECEIVED, S.DIAGNOSING),
    (S.DIAGNOSING, S.REMEDIATION_PENDING),
    (S.REMEDIATION_PENDING, S.VALIDATING),
    (S.VALIDATING, S.AWAITING_APPROVAL),
    (S.VALIDATING, S.EXECUTING),
    (S.AWAITING_APPROVAL, S.EXECUTING),
    (S.AWAITING_APPROVAL, S.REMEDIATION_PENDING),
    (S.EXECUTING, S.VERIFYING),
    (S.EXECUTING, S.DIAGNOSING),
    (S.EXECUTING, S.FAILED),
    (S.VERIFYING, S.RECOVERED),
    (S.VERIFYING, S.REMEDIATION_PENDING),
    (S.VERIFYING, S.ESCALATED),
    (S.FAILED, S.DIAGNOSING),
}

REJECTED_EDGES = [
    (S.RECEIVED, S.REMEDIATION_PENDING),
    (S.RECEIVED, S.EXECUTING),
    (S.RECEIVED, S.RECOVERED),
    (S.DIAGNOSING, S.RECEIVED),
    (S.DIAGNOSING, S.EXECUTING),
    (S.DIAGNOSING, S.RECOVERED),
    (S.REMEDIATION_PENDING, S.DIAGNOSING),
    (S.REMEDIATION_PENDING, S.EXECUTING),
    (S.VALIDATING, S.DIAGNOSING),
    (S.VALIDATING, S.REMEDIATION_PENDING),
    (S.AWAITING_APPROVAL, S.VALIDATING),
    (S.AWAITING_APPROVAL, S.RECOVERED),
    (S.EXECUTING, S.RECEIVED),
    (S.EXECUTING, S.AWAITING_APPROVAL),
    (S.VERIFYING, S.DIAGNOSING),
    (S.VERIFYING, S.EXECUTING),
    (S.RECOVERED, S.DIAGNOSING),
    (S.ESCALATED, S.DIAGNOSING),
    (S.ESCALATED, S.FAILED),
    (S.FAILED, S.RECEIVED),
    (S.FAILED, S.RECOVERED),
]


class TestEdgeMap:
    """Pure edge-map sanity: no DB involved."""

    def test_every_documented_workflow_edge_is_allowed(self):
        for src, dst in WORKFLOW_EDGES:
            assert is_allowed(src, dst), f"{src} -> {dst} should be allowed"

    def test_every_non_terminal_can_escalate_or_fail(self):
        for status in IncidentStatus:
            if status in TERMINAL_STATUSES:
                continue
            assert is_allowed(status, S.ESCALATED)
            assert is_allowed(status, S.FAILED)

    def test_terminal_statuses(self):
        assert TERMINAL_STATUSES == frozenset({S.RECOVERED, S.ESCALATED, S.FAILED})
        assert ALLOWED_TRANSITIONS[S.RECOVERED] == frozenset()
        assert ALLOWED_TRANSITIONS[S.ESCALATED] == frozenset()
        # FAILED is terminal for the run but re-enterable for rediagnosis.
        assert ALLOWED_TRANSITIONS[S.FAILED] == frozenset({S.DIAGNOSING})

    @pytest.mark.parametrize("src,dst", REJECTED_EDGES)
    def test_illegal_edges_rejected(self, src, dst):
        assert not is_allowed(src, dst), f"{src} -> {dst} should be rejected"


def _make_incident(session, status: IncidentStatus, seq: int) -> models.Incident:
    """Insert an incident directly at `status` - test setup, so the machine
    itself is bypassed deliberately."""
    incident = models.Incident(
        incident_id=f"INC-test{seq:04d}",
        fingerprint=f"{seq:064x}",
        service="payment-api",
        environment="sandbox",
        severity="medium",
        status=status,
    )
    session.add(incident)
    session.flush()
    return incident


@pytest.mark.integration
class TestTransitionPersistence:
    """DB-backed: every legal transition persists a row with a cause."""

    def test_every_legal_transition_persists_row_with_cause(self, pg):
        seq = 0
        edge_count = sum(len(targets) for targets in ALLOWED_TRANSITIONS.values())
        with session_scope(pg) as session:
            for src, targets in ALLOWED_TRANSITIONS.items():
                for dst in sorted(targets):
                    seq += 1
                    incident = _make_incident(session, src, seq)
                    row = transition(incident, dst, cause=f"test {src} -> {dst}", session=session)
                    assert row.from_status == src
                    assert row.to_status == dst
                    assert row.cause == f"test {src} -> {dst}"
                    assert incident.status == dst

        with session_scope(pg) as session:
            rows = session.scalars(select(models.Transition)).all()
            assert len(rows) == edge_count
            for row in rows:
                assert row.cause
                assert row.created_at is not None
                assert row.from_status in IncidentStatus
                assert row.to_status in IncidentStatus

    def test_status_update_and_row_commit_atomically(self, pg):
        with session_scope(pg) as session:
            incident = _make_incident(session, S.RECEIVED, 1)
            transition(incident, S.DIAGNOSING, cause="coordinator start", session=session)
        # Fresh session: both the status and the row must have landed.
        with session_scope(pg) as session:
            stored = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == "INC-test0001")
            )
            assert stored.status == S.DIAGNOSING
            rows = session.scalars(
                select(models.Transition).where(models.Transition.incident_id == stored.id)
            ).all()
            assert len(rows) == 1
            assert rows[0].cause == "coordinator start"

    def test_illegal_transition_raises_and_persists_nothing(self, pg):
        with pytest.raises(InvalidTransitionError):
            with session_scope(pg) as session:
                incident = _make_incident(session, S.VALIDATING, 1)
                transition(incident, S.DIAGNOSING, cause="nope", session=session)
        with session_scope(pg) as session:
            assert session.scalar(select(func.count(models.Transition.id))) == 0

    def test_terminal_incident_cannot_transition(self, pg):
        with pytest.raises(InvalidTransitionError):
            with session_scope(pg) as session:
                incident = _make_incident(session, S.RECOVERED, 1)
                transition(incident, S.DIAGNOSING, cause="again", session=session)

    def test_failed_reenters_diagnosing(self, pg):
        with session_scope(pg) as session:
            incident = _make_incident(session, S.FAILED, 1)
            transition(incident, S.DIAGNOSING, cause="rediagnosis", session=session)
        with session_scope(pg) as session:
            stored = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == "INC-test0001")
            )
            assert stored.status == S.DIAGNOSING
