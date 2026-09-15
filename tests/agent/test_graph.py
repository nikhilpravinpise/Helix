"""Graph composition, coordinator transitions, and run-guard tests.

Unit tier: SQLite state store, fixture-backed capabilities, FakeLLM.
The Postgres checkpointer and the run-guard's partial unique index are
exercised in tests/agent/test_integration.py (integration tier).
"""

import pytest
from fakes import K8S_SIGNAL, FakeLLM, make_deps
from sqlalchemy import select

from agent.agents.diagnosis import EvidencePlan
from agent.agents.planner import CandidateList, CandidateProposal
from agent.graph import build_graph
from agent.runner import RunConflictError, run_incident, start_run
from agent.state import Hypothesis
from helix.llm import LLMError
from state import models
from state.intake import intake_signal
from state.machine import IncidentStatus


def _make_incident(sessions, signal: dict) -> str:
    with sessions() as session:
        return intake_signal(session, signal).incident_id


def _one_plan() -> CandidateList:
    """A minimal planner proposal for graph-level tests: one catalogue
    action with valid params and a workload-surface match, so it clears
    the select floor even citing no evidence."""
    return CandidateList(
        candidates=[
            CandidateProposal(
                action="reapply_manifest",
                params={"path": "sandbox/payment-api/k8s/04-deployment.yaml"},
                rationale="re-assert the deployment manifest",
                evidence_refs=[],
            )
        ]
    )


def _transition_pairs(session, incident_id: str) -> list[tuple]:
    rows = session.scalars(
        select(models.Transition)
        .join(models.Incident)
        .where(models.Incident.incident_id == incident_id)
        .order_by(models.Transition.id)
    ).all()
    return [(r.from_status.value if r.from_status else None, r.to_status.value) for r in rows]


def _rows(sessions, model, incident_id: str):
    with sessions() as session:
        return list(
            session.scalars(
                select(model)
                .join(models.Incident)
                .where(models.Incident.incident_id == incident_id)
                .order_by(model.id)
            )
        )


class TestArmComposition:
    """D8: HELIX_ARM picks the node set - a composition switch, not code
    forks. Arm A/B node bodies landed in 1.11; this class pins the
    composition itself."""

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
    """The happy path: RECEIVED -> DIAGNOSING -> REMEDIATION_PENDING ->
    VALIDATING (1.6), each hop persisted as a transitions row, and ALL of
    them written by the coordinator (worker nodes never touch status)."""

    def test_run_reaches_verify_and_escalates_when_still_broken(self, sqlite_sessions, store):
        # 1.9: the run continues past validating
        # through the policy gate, execution, AND the real verify node -
        # the crashloop fixture still shows the fault, so verification
        # fails, the retryable/compensable action replans, and the
        # exhausted FakeLLM's empty plan escalates. The reapply_manifest
        # candidate is medium-tier in sandbox -> ALLOW_WITH_CHECKS ->
        # executed through the recording fake write transport.
        llm = FakeLLM(
            [
                EvidencePlan(requests=[]),
                Hypothesis(claim="crashloop on blank secret", confidence=0.8),
                _one_plan(),
            ]
        )
        deps, caps = make_deps(sqlite_sessions, llm, "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        assert result.run_status == models.RunStatus.TERMINAL
        assert result.error is None
        with sqlite_sessions() as session:
            pairs = _transition_pairs(session, incident_id)
        # intake_signal's creation row, then the coordinator hops -
        # nothing else writes transitions.
        assert pairs == [
            (None, "received"),
            ("received", "diagnosing"),
            ("diagnosing", "remediation_pending"),
            ("remediation_pending", "validating"),
            ("validating", "executing"),
            ("executing", "verifying"),
            ("verifying", "remediation_pending"),
            ("remediation_pending", "escalated"),
        ]
        assert result.hypothesis is not None
        assert result.selected_action is not None
        # The manifest apply went through the recording fake, not a
        # cluster - and 1.10 ran the compensating reapply (the failed
        # verification's debt) before the replan.
        assert caps["kube_write"].methods() == ["apply_manifest", "apply_manifest"]
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert incident.status == IncidentStatus.ESCALATED
        # The verify pass was persisted: failed against the crashloop
        # fixture, and reapply_manifest is compensable so the debt was
        # recorded - and paid by the compensation executions row.
        verifications = _rows(sqlite_sessions, models.Verification, incident_id)
        assert len(verifications) == 1
        assert verifications[0].outcome == models.VerificationOutcome.FAIL
        assert verifications[0].needs_compensation is True
        executions = _rows(sqlite_sessions, models.Execution, incident_id)
        assert [e.compensates_id for e in executions] == [None, executions[0].id]

    def test_empty_plan_escalates_through_coordinator(self, sqlite_sessions, store):
        llm = FakeLLM(
            [
                EvidencePlan(requests=[]),
                Hypothesis(claim="mystery fault", confidence=0.8),
                CandidateList(candidates=[]),
            ]
        )
        deps, _ = make_deps(sqlite_sessions, llm, "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        with sqlite_sessions() as session:
            pairs = _transition_pairs(session, incident_id)
        assert pairs[-1] == ("remediation_pending", "escalated")

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
        llm = FakeLLM(
            [
                EvidencePlan(requests=[]),
                Hypothesis(claim="crashloop on blank secret", confidence=0.8),
                _one_plan(),
            ]
        )
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

        # 1.9: the run now continues through validation + execution into
        # verification, which fails against the still-broken fixture -
        # replan -> empty -> escalate (reapply_manifest is medium-tier in
        # sandbox).
        assert result.status == IncidentStatus.ESCALATED
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            # 1.10 attempt accounting: the failed -> diagnosing re-entry
            # consumes one attempt, and the failed verification's replan
            # consumes the next.
            assert incident.attempt_count == 2
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

        # An unscripted FakeLLM answers the plan call with an empty
        # CandidateList -> the planner escalates -> ESCALATED (1.6).
        first = run_incident(incident_id, deps=deps)
        second = run_incident(incident_id, deps=deps)

        assert first.run_status == models.RunStatus.TERMINAL
        assert second.run_status == models.RunStatus.TERMINAL
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert len(incident.workflow_runs) == 2
            assert incident.status == IncidentStatus.ESCALATED
