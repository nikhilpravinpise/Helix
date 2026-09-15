"""Graph composition, coordinator transitions, and run-guard tests.

Unit tier: SQLite state store, fixture-backed capabilities, FakeLLM.
The Postgres checkpointer and the run-guard's partial unique index are
exercised in tests/agent/test_integration.py (integration tier).
"""

import pytest
from fakes import K8S_SIGNAL, FakeLLM, make_deps
from sqlalchemy import select

from agent.agents.diagnosis import EvidencePlan
from agent.graph import build_graph
from agent.runner import RunConflictError, run_incident, start_run
from helix.llm import LLMError
from state import models
from state.intake import intake_signal
from state.machine import IncidentStatus


def _make_incident(sessions, signal: dict) -> str:
    with sessions() as session:
        return intake_signal(session, signal).incident_id


def _transition_pairs(session, incident_id: str) -> list[tuple]:
    rows = session.scalars(
        select(models.Transition)
        .join(models.Incident)
        .where(models.Incident.incident_id == incident_id)
        .order_by(models.Transition.id)
    ).all()
    return [(r.from_status.value if r.from_status else None, r.to_status.value) for r in rows]


class TestArmComposition:
    """D8: HELIX_ARM picks the node set - a composition switch, not code
    forks. Arm A/B bodies are placeholders until 1.11."""

    def test_arm_e_has_full_node_slots(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        nodes = set(build_graph(deps, arm="E").get_graph().nodes)
        assert {
            "intake",
            "coordinator",
            "diagnosis",
            "plan",
            "validate",
            "execute",
            "verify",
        } <= nodes

    def test_arm_b_collapses_diagnosis_and_drops_validate_verify(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        nodes = set(build_graph(deps, arm="B").get_graph().nodes)
        assert "diagnose_and_plan" in nodes
        assert "diagnosis" not in nodes
        assert "plan" not in nodes
        assert "validate" not in nodes
        assert "verify" not in nodes
        assert "execute" in nodes

    def test_arm_a_uses_rules_variants(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        nodes = set(build_graph(deps, arm="A").get_graph().nodes)
        assert "rules_diagnosis" in nodes
        assert "rules_plan" in nodes
        assert "diagnosis" not in nodes
        assert "diagnose_and_plan" not in nodes

    def test_unknown_arm_rejected(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        with pytest.raises(ValueError):
            build_graph(deps, arm="Z")


class TestCoordinatorFlow:
    """The happy path: RECEIVED -> DIAGNOSING -> REMEDIATION_PENDING, each
    hop persisted as a transitions row, and ALL of them written by the
    coordinator (worker nodes never touch status)."""

    def test_run_reaches_remediation_pending_with_transition_rows(self, sqlite_sessions, store):
        llm = FakeLLM([EvidencePlan(requests=[])])  # no extra evidence needed
        deps, _ = make_deps(sqlite_sessions, llm, "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.REMEDIATION_PENDING
        assert result.run_status == models.RunStatus.TERMINAL
        assert result.error is None
        with sqlite_sessions() as session:
            pairs = _transition_pairs(session, incident_id)
        # intake_signal's creation row, then the two coordinator hops -
        # nothing else writes transitions.
        assert pairs == [
            (None, "received"),
            ("received", "diagnosing"),
            ("diagnosing", "remediation_pending"),
        ]
        assert result.hypothesis is not None
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert incident.status == IncidentStatus.REMEDIATION_PENDING

    def test_diagnosis_failure_fails_incident_through_coordinator(self, sqlite_sessions, store):
        class DownLLM(FakeLLM):
            def complete_structured(self, prompt, schema, *, max_tokens=2048):
                raise LLMError("provider down")

        deps, _ = make_deps(sqlite_sessions, DownLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        # The node records errors; the coordinator performs the
        # diagnosing -> failed transition - failure flows through the
        # same single transition owner as success.
        assert result.status == IncidentStatus.FAILED
        with sqlite_sessions() as session:
            pairs = _transition_pairs(session, incident_id)
        assert pairs == [
            (None, "received"),
            ("received", "diagnosing"),
            ("diagnosing", "failed"),
        ]

    def test_failed_incident_reenters_diagnosis(self, sqlite_sessions, store):
        llm = FakeLLM([EvidencePlan(requests=[])])
        deps, _ = make_deps(sqlite_sessions, llm, "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        # Drive to FAILED directly through the machine (as a crashed run
        # would leave it), then re-run: the failed -> diagnosing re-entry
        # is coordinator-owned and bumps attempt_count.
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            from state.machine import transition

            transition(incident, IncidentStatus.DIAGNOSING, cause="t", session=session)
            transition(incident, IncidentStatus.FAILED, cause="t", session=session)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.REMEDIATION_PENDING
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert incident.attempt_count == 1
            pairs = _transition_pairs(session, incident_id)
        assert ("failed", "diagnosing") in pairs
        # The rediagnosis hypothesis is tagged with the attempt.
        assert result.hypothesis["attempt"] == 1

    def test_terminal_incident_run_is_noop(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            from state.machine import transition

            transition(incident, IncidentStatus.DIAGNOSING, cause="t", session=session)
            transition(incident, IncidentStatus.ESCALATED, cause="t", session=session)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        assert result.run_status == models.RunStatus.TERMINAL
        # No LLM call: the coordinator saw a terminal status and ended.
        assert all(c[0] != "structured" for c in deps.llm.calls)


class TestRunGuard:
    """The single-active-run guard (basic form; hardened in 1.10)."""

    def test_second_running_run_refused(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            start_run(session, incident)
            with pytest.raises(RunConflictError):
                start_run(session, incident)

    def test_run_incident_refuses_while_running(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            start_run(session, incident)

        with pytest.raises(RunConflictError):
            run_incident(incident_id, deps=deps)

    def test_running_uniqueness_enforced_by_index(self, sqlite_sessions, store):
        """The partial unique index is declared on the model so
        create_all-built stores enforce it too - the guard is a database
        guarantee, not only a check-then-act."""
        from sqlalchemy.exc import IntegrityError

        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            session.add(
                models.WorkflowRun(incident_id=incident.id, status=models.RunStatus.RUNNING)
            )

        with pytest.raises(IntegrityError):
            with sqlite_sessions() as session:
                session.add(
                    models.WorkflowRun(incident_id=incident.id, status=models.RunStatus.RUNNING)
                )

    def test_completed_run_allows_new_run(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        first = run_incident(incident_id, deps=deps)
        second = run_incident(incident_id, deps=deps)

        assert first.run_status == models.RunStatus.TERMINAL
        assert second.run_status == models.RunStatus.TERMINAL
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert len(incident.workflow_runs) == 2
            assert incident.status == IncidentStatus.REMEDIATION_PENDING
