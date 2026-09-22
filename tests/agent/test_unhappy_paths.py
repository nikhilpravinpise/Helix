"""Subphase 1.10: unhappy paths and bounded recovery - compensation,
per-incident retry budgets, approval timeouts, step timeouts, run
concurrency, and execution idempotency.

Unit tier throughout: SQLite state, fixture evidence (plus synthetic
fixture documents built in-test), scripted FakeLLM, recording fake write
transports. The Postgres-only concurrency legs (the `running` run-row
guard under a real lock) live at the bottom behind the `integration`
mark.
"""

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fakes import JENKINS_SIGNAL, K8S_SIGNAL, FakeLLM, Recording, make_deps
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agent.agents.diagnosis import EvidencePlan
from agent.agents.execution import execution_node
from agent.agents.planner import CandidateList, CandidateProposal
from agent.execution import persist_execution
from agent.policy import decide
from agent.runner import RunConflictError, run_incident
from agent.state import Hypothesis
from agent.validation import candidate_fingerprint
from integrations.fakes import FakeKubernetesWrite
from integrations.writes import WriteError, WriteRefusedError
from state import models
from state.approvals import (
    ApprovalDecidedError,
    pending_for_incident,
    record_decision,
    sweep_approval_timeout,
)
from state.intake import intake_signal
from state.machine import IncidentStatus, transition
from state.provenance import assemble_provenance, render_markdown

DEPLOYMENT_PATH = "sandbox/payment-api/k8s/04-deployment.yaml"
CONFIGMAP_PATH = "sandbox/payment-api/k8s/02-configmap.yaml"
SECRET_PATH = "sandbox/payment-api/k8s/03-secret.yaml"


def _make_incident(sessions, signal: dict) -> str:
    with sessions() as session:
        return intake_signal(session, signal).incident_id


def _llm(plans: list[CandidateList], claim: str = "payment-api is wedged") -> FakeLLM:
    return FakeLLM(
        [
            EvidencePlan(requests=[]),
            Hypothesis(claim=claim, evidence_refs=[], confidence=0.8),
            *plans,
        ]
    )


def _plan(action: str, params: dict, rationale: str = "fix it") -> CandidateList:
    return CandidateList(
        candidates=[CandidateProposal(action=action, params=params, rationale=rationale)]
    )


def _scale_plan(replicas: int) -> CandidateList:
    return _plan(
        "scale_deployment",
        {"deployment": "payment-api", "namespace": "payment-api", "replicas": replicas},
        "restore the replica count",
    )


#: A claim that hits the ranker's "capacity" surface - the deterministic
#: surface match scale_deployment needs to score over the safe bar.
_CAPACITY_CLAIM = "payment-api scaled to a bad replica count - zero pods"


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


def _executions(sessions, incident_id: str):
    return _rows(sessions, models.Execution, incident_id)


def _transitions(sessions, incident_id: str) -> list[tuple]:
    rows = _rows(sessions, models.Transition, incident_id)
    return [(r.from_status.value if r.from_status else None, r.to_status.value) for r in rows]


def _transition_causes(sessions, incident_id: str) -> list[str]:
    return [r.cause for r in _rows(sessions, models.Transition, incident_id)]


def _incident(sessions, incident_id: str) -> models.Incident:
    with sessions() as session:
        return session.scalar(
            select(models.Incident).where(models.Incident.incident_id == incident_id)
        )


# --- Synthetic fixture documents ---------------------------------------


def _pod(name="payment-api-aa-1", *, app="payment-api", ready=True, waiting=None):
    """A kubectl-pod-shaped dict as FixtureKubernetesEvidence consumes."""
    state = (
        {"waiting": {"reason": waiting}}
        if waiting
        else {"running": {"startedAt": "2026-09-18T00:00:00Z"}}
    )
    return {
        "metadata": {
            "name": name,
            "namespace": "payment-api",
            "labels": {"app": app},
            "ownerReferences": [{"controller": True, "kind": "ReplicaSet", "name": f"{app}-rs"}],
        },
        "spec": {"containers": [{"name": "payment-api", "image": "payment-api:kind"}]},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "payment-api",
                    "image": "payment-api:kind",
                    "ready": ready,
                    "restartCount": 0,
                    "state": state,
                    "lastState": {},
                }
            ],
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "startTime": "2026-09-18T00:00:00Z",
        },
    }


def _deployment(name="payment-api", *, replicas=1, ready=1, updated=1, available=1):
    return {
        "metadata": {"name": name, "namespace": "payment-api"},
        "spec": {"replicas": replicas},
        "status": {
            "replicas": replicas,
            "readyReplicas": ready,
            "updatedReplicas": updated,
            "availableReplicas": available,
            "observedGeneration": 2,
            "conditions": [
                {"type": "Available", "status": "True" if ready else "False"},
                {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"},
            ],
        },
    }


def _broken_doc(replicas: int = 1):
    """A still-broken workload the scale test verifies against: one
    crash-looping pod and a deployment that reports the pre-scale replica
    count (so the executor can capture `previous_replicas`)."""
    return {
        "fault": "synthetic-broken",
        "evidence": {
            "pods_payment_api": {"items": [_pod(waiting="CrashLoopBackOff", ready=False)]},
            "deployments_payment_api": _deployment(
                replicas=replicas, ready=0, updated=0, available=0
            ),
            "events": {"items": []},
            "logs_payment_api": [
                '2026-09-18T00:00:01Z [payment-api-aa-1] INFO: "GET /ready" 200 OK'
            ],
        },
    }


def _seed_executed(
    sessions,
    signal: dict,
    *,
    status: IncidentStatus = IncidentStatus.VERIFYING,
    action: str,
    params: dict,
    output: dict | None,
    verification: bool = False,
    needs_compensation: bool = False,
) -> str:
    """Seed an incident at `status` (executing or verifying) with a
    selected candidate, its executions row, and optionally a recorded
    FAIL verification row (the durable compensation debt a crashed run
    would leave behind)."""
    incident_id = _make_incident(sessions, signal)
    now = datetime.now(UTC)
    with sessions() as session:
        incident = session.scalar(
            select(models.Incident).where(models.Incident.incident_id == incident_id)
        )
        for step in (
            IncidentStatus.DIAGNOSING,
            IncidentStatus.REMEDIATION_PENDING,
            IncidentStatus.VALIDATING,
            IncidentStatus.EXECUTING,
            IncidentStatus.VERIFYING,
        ):
            transition(incident, step, cause="test setup", session=session)
            if step == status:
                break
        cand = models.Candidate(
            incident_id=incident.id,
            attempt=0,
            rank=1,
            action=action,
            params=params,
            rationale="seeded",
            evidence_refs=[],
            score=0.9,
            risk_tier="medium",
            selected=True,
            components={},
        )
        session.add(cand)
        session.flush()
        fp = candidate_fingerprint(action, params)
        execution = models.Execution(
            incident_id=incident.id,
            candidate_id=cand.id,
            attempt=0,
            action=action,
            params=params,
            fingerprint=fp,
            policy_decision="allow_with_checks",
            verification_required=True,
            origin="fixture",
            result=models.ExecutionResult.SUCCESS,
            detail="seeded execution",
            output=output,
            started_at=now,
            finished_at=now,
        )
        session.add(execution)
        session.flush()
        if verification:
            session.add(
                models.Verification(
                    incident_id=incident.id,
                    execution_id=execution.id,
                    candidate_id=cand.id,
                    attempt=0,
                    action=action,
                    params=params,
                    fingerprint=fp,
                    outcome=models.VerificationOutcome.FAIL,
                    detail="seeded verification failure",
                    checks=[],
                    window_seconds=1.0,
                    poll_seconds=1.0,
                    needs_compensation=needs_compensation,
                    started_at=now,
                    finished_at=now,
                )
            )
    return incident_id


class TestCompensation:
    """The compensate node: a failed verification on a compensable action
    runs the catalogue's compensating_action before the coordinator
    replans or escalates."""

    def test_scale_failure_scales_back_to_previous_replicas(self, sqlite_sessions, store):
        """scale_deployment to 2 fails verification (pods still broken);
        the compensation is scale back to the replica count the executor
        captured at write time - never a guessed number."""
        deps, caps = make_deps(
            sqlite_sessions,
            _llm([_scale_plan(2)], claim=_CAPACITY_CLAIM),
            _broken_doc(replicas=1),
            store,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        # Verify failed, the undo ran, then the empty replan escalated.
        assert result.status == IncidentStatus.ESCALATED
        assert caps["kube_write"].calls == [
            ("scale", ("payment-api", "payment-api", 2), {}),
            ("scale", ("payment-api", "payment-api", 1), {}),
        ]
        rows = _executions(sqlite_sessions, incident_id)
        assert len(rows) == 2
        assert rows[0].output["previous_replicas"] == 1
        assert rows[1].action == "scale_deployment"
        assert rows[1].params["replicas"] == 1
        assert rows[1].compensates_id == rows[0].id
        assert rows[1].result == models.ExecutionResult.SUCCESS
        assert _transitions(sqlite_sessions, incident_id)[-2:] == [
            ("verifying", "remediation_pending"),
            ("remediation_pending", "escalated"),
        ]

    def test_compensation_transport_error_escalates(self, sqlite_sessions, store):
        """A compensation that fails is the human-required case: the
        attempt is recorded, the debt stays unpaid, the incident
        escalates - the compensation itself is never retried in a loop."""

        class FailsOnScaleBack(FakeKubernetesWrite):
            def scale(self, deployment, namespace, replicas):
                if replicas == 1:
                    raise WriteError("cluster API unavailable")
                return super().scale(deployment, namespace, replicas)

        deps, _caps = make_deps(
            sqlite_sessions,
            _llm([_scale_plan(2)], claim=_CAPACITY_CLAIM),
            _broken_doc(replicas=1),
            store,
        )
        deps = replace(deps, kubernetes_write=Recording(FailsOnScaleBack()))
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        rows = _executions(sqlite_sessions, incident_id)
        assert len(rows) == 2
        assert rows[1].compensates_id == rows[0].id
        assert rows[1].result == models.ExecutionResult.ERROR
        assert "cluster API unavailable" in rows[1].detail
        causes = _transition_causes(sqlite_sessions, incident_id)
        assert any("compensation for scale_deployment failed" in c for c in causes)
        # The debt is recorded and still unpaid - the escalation carries it.
        verifications = _rows(sqlite_sessions, models.Verification, incident_id)
        assert verifications[-1].needs_compensation is True

    def test_compensation_underivable_escalates(self, sqlite_sessions, store):
        """A scale execution row without previous_replicas cannot derive
        its undo - refused (never guessed), recorded, escalated."""
        params = {"deployment": "payment-api", "namespace": "payment-api", "replicas": 2}
        incident_id = _seed_executed(
            sqlite_sessions,
            K8S_SIGNAL,
            action="scale_deployment",
            params=params,
            output={"op": "scale", "deployment": "payment-api"},  # no previous_replicas
            verification=True,
            needs_compensation=True,
        )
        deps, caps = make_deps(sqlite_sessions, FakeLLM([]), _broken_doc(), store)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        rows = _executions(sqlite_sessions, incident_id)
        comp = rows[-1]
        assert comp.compensates_id == rows[0].id
        assert comp.result == models.ExecutionResult.REFUSED
        assert "previous_replicas" in comp.detail
        # Nothing was dispatched - no undo was fired at the fake.
        assert caps["kube_write"].calls == []
        causes = _transition_causes(sqlite_sessions, incident_id)
        assert any("compensation for scale_deployment failed" in c for c in causes)

    def test_compensation_is_one_shot_per_failed_execution(self, sqlite_sessions, store):
        """A crashed run that recorded a refused/errored compensation row
        replays that outcome on resume - the debt stays unpaid and the
        coordinator escalates, but the undo is never fired twice."""
        params = {"deployment": "payment-api", "namespace": "payment-api", "replicas": 2}
        incident_id = _seed_executed(
            sqlite_sessions,
            K8S_SIGNAL,
            action="scale_deployment",
            params=params,
            output={"op": "scale", "previous_replicas": 1},
            verification=True,
            needs_compensation=True,
        )
        # The first pass's refused comp row - as if the run crashed right
        # after persisting it.
        with sqlite_sessions() as session:
            pk = session.scalar(
                select(models.Incident.id).where(models.Incident.incident_id == incident_id)
            )
            failed = session.scalar(
                select(models.Execution).where(models.Execution.incident_id == pk)
            )
            now = datetime.now(UTC)
            session.add(
                models.Execution(
                    incident_id=pk,
                    compensates_id=failed.id,
                    attempt=0,
                    action="scale_deployment",
                    params={**params, "replicas": 1},
                    fingerprint=candidate_fingerprint(
                        "scale_deployment", {**params, "replicas": 1}
                    ),
                    policy_decision="compensation",
                    origin="fixture",
                    result=models.ExecutionResult.REFUSED,
                    detail="refused by earlier pass",
                    output=None,
                    started_at=now,
                    finished_at=now,
                )
            )
        deps, caps = make_deps(sqlite_sessions, FakeLLM([]), _broken_doc(), store)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        assert caps["kube_write"].calls == []  # the undo was not re-fired
        rows = _executions(sqlite_sessions, incident_id)
        assert len(rows) == 2  # seeded remediation + recorded comp, no third row
        causes = _transition_causes(sqlite_sessions, incident_id)
        assert any("compensation for scale_deployment failed" in c for c in causes)

    def test_open_fix_pr_compensates_with_close_pr(self, sqlite_sessions, store):
        """The executor-side op: a failed open_fix_pr withdraws its PR via
        close_pr - a write the planner can never propose - recorded as a
        compensation row."""
        params = {
            "incident_id": "INC-x",
            "branch": "helix/INC-1",
            "files": [{"path": DEPLOYMENT_PATH, "content": "..."}],
            "title": "fix",
        }
        incident_id = _seed_executed(
            sqlite_sessions,
            K8S_SIGNAL,
            action="open_fix_pr",
            params=params,
            # The recorded write covered no files - the verification's
            # pull_request leg fails on the coverage mismatch.
            output={"op": "open_fix_pr", "branch": "helix/INC-1", "files": [], "recorded": True},
        )
        deps, caps = make_deps(sqlite_sessions, FakeLLM([]), "cluster/crashloop", store)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        assert caps["github_write"].calls == [
            ("close_pr", (), {"branch": "helix/INC-1", "pr_number": None})
        ]
        rows = _executions(sqlite_sessions, incident_id)
        assert len(rows) == 2
        assert rows[1].action == "close_pr"
        assert rows[1].compensates_id == rows[0].id
        assert rows[1].result == models.ExecutionResult.SUCCESS
        assert rows[1].policy_decision == "compensation"

    def test_irreversible_failure_escalates_without_compensation(self, monkeypatch):
        """An irreversible action's failed verification escalates
        directly - nothing can be compensated. (No catalogue action is
        irreversible yet, so the test registers a synthetic spec - same
        seam as test_verification's routing tests.)"""
        from agent.actions.catalogue import Reversibility, get_action
        from agent.agents.coordinator import _decide

        spec = replace(get_action("restart_deployment"), reversibility=Reversibility.IRREVERSIBLE)
        monkeypatch.setattr(
            "agent.agents.coordinator.get_action",
            lambda name: spec if name == "restart_deployment" else get_action(name),
        )
        state = {
            "status": IncidentStatus.VERIFYING,
            "verification": {
                "outcome": "fail",
                "action": "restart_deployment",
                "fingerprint": "fp",
                "detail": "still broken",
            },
            "needs_compensation": False,
            "attempts": 0,
        }
        decision = _decide(state, None, max_attempts=3)
        assert decision.to_status == IncidentStatus.ESCALATED
        assert "irreversible" in decision.cause
        assert not decision.replan

    def test_stateless_failure_replans_without_compensation(self, sqlite_sessions, store):
        """rerun_pipeline is stateless - a failed verification replans
        directly, no compensate hop, no compensation row."""
        deps, caps = make_deps(
            sqlite_sessions,
            _llm([_plan("rerun_pipeline", {"job": "payment-api"})]),
            "ci/test-regression",
            store,
        )
        incident_id = _make_incident(sqlite_sessions, JENKINS_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        rows = _executions(sqlite_sessions, incident_id)
        assert len(rows) == 1
        assert rows[0].action == "rerun_pipeline"
        assert rows[0].compensates_id is None
        verifications = _rows(sqlite_sessions, models.Verification, incident_id)
        assert verifications[0].needs_compensation is False


class TestRetryBudget:
    """HELIX_MAX_ATTEMPTS bounds every failed-cycle loop (1.10)."""

    def test_verify_failures_consume_attempts_then_escalate(self, sqlite_sessions, store):
        """Two failed cycles with max_attempts=2: the second verification
        failure's replan decision escalates on the budget instead of
        planning a third cycle."""
        deps, _caps = make_deps(
            sqlite_sessions,
            _llm([_scale_plan(2), _scale_plan(3)], claim=_CAPACITY_CLAIM),
            _broken_doc(replicas=1),
            store,
            max_attempts=2,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        incident = _incident(sqlite_sessions, incident_id)
        assert incident.attempt_count == 2
        rows = _executions(sqlite_sessions, incident_id)
        # Each failed cycle is a remediation + its compensation.
        assert len(rows) == 4
        assert [r.compensates_id is not None for r in rows] == [False, True, False, True]
        causes = _transition_causes(sqlite_sessions, incident_id)
        assert any("retry budget exhausted" in c for c in causes)
        assert _transitions(sqlite_sessions, incident_id)[-1] == ("verifying", "escalated")

    def test_failed_reentry_within_budget_rediagnoses(self, sqlite_sessions, store):
        """A failed incident re-run under budget takes failed ->
        diagnosing and consumes exactly one attempt."""
        deps, _caps = make_deps(
            sqlite_sessions,
            _llm([CandidateList(candidates=[])]),  # the replan escalates
            "cluster/crashloop",
            store,
            max_attempts=2,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            for status in (
                IncidentStatus.DIAGNOSING,
                IncidentStatus.REMEDIATION_PENDING,
                IncidentStatus.FAILED,
            ):
                transition(incident, status, cause="test setup", session=session)
            incident.attempt_count = 0

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        incident = _incident(sqlite_sessions, incident_id)
        assert incident.attempt_count == 1
        pairs = _transitions(sqlite_sessions, incident_id)
        assert ("failed", "diagnosing") in pairs

    def test_failed_reentry_at_budget_escalates_without_rerunning(self, sqlite_sessions, store):
        """The budget guards the failed -> diagnosing edge too: a failed
        incident whose attempts are spent escalates immediately - no
        rediagnosis is even attempted."""
        deps, _caps = make_deps(
            sqlite_sessions, _llm([]), "cluster/crashloop", store, max_attempts=2
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            for status in (
                IncidentStatus.DIAGNOSING,
                IncidentStatus.REMEDIATION_PENDING,
                IncidentStatus.FAILED,
            ):
                transition(incident, status, cause="test setup", session=session)
            incident.attempt_count = 1  # one cycle consumed; budget is 2

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        pairs = _transitions(sqlite_sessions, incident_id)
        assert ("failed", "escalated") in pairs
        causes = _transition_causes(sqlite_sessions, incident_id)
        assert any("retry budget exhausted" in c for c in causes)

    def test_execution_failure_routes_to_rediagnosis(self, sqlite_sessions, store):
        """A write transport error lands the run at failed; the next run
        rediagnoses with the failed fingerprint excluded so the replan
        cannot repeat the same call."""

        class DownKube(FakeKubernetesWrite):
            def apply_manifest(self, path):
                raise WriteError("cluster unreachable")

        plan1 = _plan("reapply_manifest", {"path": DEPLOYMENT_PATH})
        # The rediagnosed plan re-proposes the failed call plus a fallback;
        # the exclusion must drop the re-proposal.
        plan2 = CandidateList(
            candidates=[
                CandidateProposal(
                    action="reapply_manifest",
                    params={"path": DEPLOYMENT_PATH},
                    rationale="try the deployment again",
                ),
                CandidateProposal(
                    action="reapply_manifest",
                    params={"path": CONFIGMAP_PATH},
                    rationale="re-assert the configmap instead",
                ),
            ]
        )
        # One queue serves both runs: run 1 pops the first three items,
        # run 2 (the failed -> diagnosing re-entry) pops the rest.
        llm = FakeLLM(
            [
                EvidencePlan(requests=[]),
                Hypothesis(claim="wedged", evidence_refs=[], confidence=0.8),
                plan1,
                EvidencePlan(requests=[]),
                # The configmap-surface claim is what lets the fallback
                # score over the bar (envconfig surface match).
                Hypothesis(
                    claim="stale configmap env config drifted",
                    evidence_refs=[],
                    confidence=0.7,
                ),
                plan2,
            ]
        )
        deps, _caps = make_deps(sqlite_sessions, llm, "cluster/crashloop", store)
        deps = replace(deps, kubernetes_write=Recording(DownKube()))
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        first = run_incident(incident_id, deps=deps)
        assert first.status == IncidentStatus.FAILED

        # Second run: the cluster is back; only the exclusion keeps the
        # failed call from repeating.
        deps = replace(deps, kubernetes_write=Recording(FakeKubernetesWrite()))
        second = run_incident(incident_id, deps=deps)

        assert second.status == IncidentStatus.ESCALATED
        rows = _executions(sqlite_sessions, incident_id)
        remediations = [r for r in rows if r.compensates_id is None]
        paths = [r.params["path"] for r in remediations]
        # The failed path is never re-executed; the fallback ran instead.
        assert DEPLOYMENT_PATH not in paths[1:]
        assert CONFIGMAP_PATH in paths
        hypotheses = _rows(sqlite_sessions, models.Hypothesis, incident_id)
        assert len(hypotheses) == 2  # the failed -> diagnosing re-entry rediagnosed
        pairs = _transitions(sqlite_sessions, incident_id)
        assert ("failed", "diagnosing") in pairs

    def test_execution_refusal_replans_in_run(self, sqlite_sessions, store):
        """REFUSED is a candidate-level verdict - the call was never made,
        so a different candidate can succeed in the SAME run (the 1.5 gap
        closed): the refused fingerprint joins the exclusion list and the
        planner re-selects, no failed -> diagnosing round-trip needed."""

        class RefuseScaleTwo(FakeKubernetesWrite):
            def scale(self, deployment, namespace, replicas):
                if replicas == 2:
                    raise WriteRefusedError("replicas=2 revoked by admission policy")
                return super().scale(deployment, namespace, replicas)

        llm = _llm([_scale_plan(2), _scale_plan(3)], claim=_CAPACITY_CLAIM)
        deps, _caps = make_deps(sqlite_sessions, llm, _broken_doc(), store)
        deps = replace(deps, kubernetes_write=Recording(RefuseScaleTwo()))
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        run_incident(incident_id, deps=deps)

        # One run, two execution attempts: the refused call excluded, the
        # fallback dispatched - the in-run replan is the thing under test;
        # where the fallback's own verification lands is fixture business
        # (recovery, a verifying replan, or escalate are all honest
        # outcomes). What must NOT appear is a failed -> diagnosing
        # round-trip: the refusal replanned without it.
        rows = _executions(sqlite_sessions, incident_id)
        remediations = [r for r in rows if r.compensates_id is None]
        assert len(remediations) == 2
        assert remediations[0].result == models.ExecutionResult.REFUSED
        assert remediations[0].params["replicas"] == 2
        assert remediations[1].params["replicas"] == 3
        pairs = _transitions(sqlite_sessions, incident_id)
        # The replan hop is visible, and there is no failed detour.
        assert ("executing", "remediation_pending") in pairs
        assert not any(t[0] == "failed" for t in pairs)

    def test_execution_refusal_replan_is_bounded(self, sqlite_sessions, store):
        """The in-run replan is bounded by HELIX_MAX_ATTEMPTS worth of
        exclusions - a selection that keeps getting refused escalates
        rather than cycling the planner forever."""

        class RefuseAllScales(FakeKubernetesWrite):
            def scale(self, deployment, namespace, replicas):
                raise WriteRefusedError(f"scale to {replicas} refused by admission policy")

        llm = _llm([_scale_plan(2), _scale_plan(3), _scale_plan(4)], claim=_CAPACITY_CLAIM)
        deps, _caps = make_deps(sqlite_sessions, llm, _broken_doc(), store)
        deps = replace(
            deps,
            kubernetes_write=Recording(RefuseAllScales()),
            max_attempts=2,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        # The exclusion bound fired: refused candidates accumulated until
        # the budget spent, then the run escalated rather than looping.
        assert result.status == IncidentStatus.ESCALATED
        causes = _transition_causes(sqlite_sessions, incident_id)
        assert any("replan budget" in c for c in causes)


class TestApprovalTimeout:
    """HELIX_APPROVAL_TIMEOUT: a pending row past the window is recorded
    rejected-by-timeout and the incident escalates - the sweep runs under
    the incident lock inside run_incident."""

    def _suspend_on_gate(self, sqlite_sessions, store):
        deps, caps = make_deps(
            sqlite_sessions,
            _llm([_plan("reapply_manifest", {"path": DEPLOYMENT_PATH})]),
            "cluster/crashloop",
            store,
        )
        # Medium-tier write against production -> REQUIRE_APPROVAL.
        incident_id = _make_incident(sqlite_sessions, {**K8S_SIGNAL, "environment": "production"})
        result = run_incident(incident_id, deps=deps)
        assert result.status == IncidentStatus.AWAITING_APPROVAL
        return deps, caps, incident_id

    def _backdate_pending(self, sessions, incident_id: str, seconds: float):
        with sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            pending = pending_for_incident(session, incident.id)
            pending.created_at = datetime.now(UTC) - timedelta(seconds=seconds)
            return pending.id

    def test_pending_approval_past_timeout_escalates(self, sqlite_sessions, store):
        deps, caps, incident_id = self._suspend_on_gate(sqlite_sessions, store)
        approval_id = self._backdate_pending(sqlite_sessions, incident_id, 7200)
        deps = replace(deps, approval_timeout=3600.0)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        pairs = _transitions(sqlite_sessions, incident_id)
        assert ("awaiting_approval", "escalated") in pairs
        causes = _transition_causes(sqlite_sessions, incident_id)
        assert any("approval timeout" in c for c in causes)
        with sqlite_sessions() as session:
            approval = session.get(models.Approval, approval_id)
            assert approval.status == models.ApprovalStatus.REJECTED
            assert approval.decided_by == "helix"
            assert "approval timeout" in (approval.note or "")
            # A timed-out row can never execute: a late decision is refused.
            with pytest.raises(ApprovalDecidedError):
                record_decision(session, approval, True, "alice")
            # The sweep's escalation carries the same projection ops as
            # any other transition - a timeout is never an invisible one.
            rows = session.scalars(
                select(models.IntegrationOutbox)
                .where(models.IntegrationOutbox.incident_id == approval.incident_id)
                .order_by(models.IntegrationOutbox.id)
            ).all()
            ops = [(r.integration, r.op) for r in rows]
            assert ("slack", "notify") in ops
            assert ("notion", "sync_page") in ops
            assert any(
                r.op == "transition_work_item" and r.payload.get("status") == "blocked"
                for r in rows
            )
        assert caps["kube_write"].calls == []

    def test_pending_approval_within_timeout_is_undisturbed(self, sqlite_sessions, store):
        deps, _caps, incident_id = self._suspend_on_gate(sqlite_sessions, store)
        deps = replace(deps, approval_timeout=3600.0)

        result = run_incident(incident_id, deps=deps)

        # The pending row is young: the run resumes and parks again.
        assert result.status == IncidentStatus.AWAITING_APPROVAL
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert pending_for_incident(session, incident.id) is not None

    def test_late_decision_on_stale_row_is_refused(self, sqlite_sessions, store):
        """The decision-side guard: a pending row older than the window
        refuses a decision even when no sweep has observed it yet - a
        timed-out approval can never execute."""
        _deps, _caps, incident_id = self._suspend_on_gate(sqlite_sessions, store)
        approval_id = self._backdate_pending(sqlite_sessions, incident_id, 7200)

        with sqlite_sessions() as session:
            approval = session.get(models.Approval, approval_id)
            with pytest.raises(ApprovalDecidedError, match="timed out"):
                record_decision(session, approval, True, "alice", timeout_s=3600.0)
            # Refusal wrote nothing: still pending until the sweep runs.
            assert approval.status == models.ApprovalStatus.PENDING

    def test_sweep_unit_edges(self, sqlite_sessions, store):
        """Direct sweep coverage: disabled timeout, non-awaiting status,
        no pending row, and a young row are all no-ops."""
        _deps, _caps, incident_id = self._suspend_on_gate(sqlite_sessions, store)
        self._backdate_pending(sqlite_sessions, incident_id, 7200)
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            # Disabled timeout: the stale row is left alone.
            assert sweep_approval_timeout(session, incident, 0) is None
            # A stale row under a generous window is left alone.
            assert sweep_approval_timeout(session, incident, 99999) is None
        # awaiting_approval WITHOUT a pending row -> no-op.
        other = _make_incident(sqlite_sessions, K8S_SIGNAL)
        with sqlite_sessions() as session:
            inc = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == other)
            )
            for step in (
                IncidentStatus.DIAGNOSING,
                IncidentStatus.REMEDIATION_PENDING,
                IncidentStatus.VALIDATING,
                IncidentStatus.AWAITING_APPROVAL,
            ):
                transition(inc, step, cause="t", session=session)
            assert sweep_approval_timeout(session, inc, 3600) is None


class TestStepTimeout:
    """HELIX_STEP_TIMEOUT bounds one node's wall-clock (1.10): a node that
    outlives it reports node_failed and the coordinator fails the run."""

    def test_step_timeout_fails_run(self, sqlite_sessions, store):
        class SlowKube(FakeKubernetesWrite):
            def apply_manifest(self, path):
                time.sleep(0.5)
                return super().apply_manifest(path)

        deps, _caps = make_deps(
            sqlite_sessions,
            _llm([_plan("reapply_manifest", {"path": DEPLOYMENT_PATH})]),
            "cluster/crashloop",
            store,
            step_timeout=0.05,
        )
        deps = replace(deps, kubernetes_write=Recording(SlowKube()))
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.FAILED
        pairs = _transitions(sqlite_sessions, incident_id)
        assert pairs[-1] == ("executing", "failed")
        causes = _transition_causes(sqlite_sessions, incident_id)
        assert any("execute" in c and "step timeout" in c for c in causes)

    def test_step_timeout_wrapper_semantics(self):
        """The wrapper itself: a fast node returns its update, a raising
        node propagates, a node's own TimeoutError is not confused with
        the deadline, and a slow node reports node_failed."""
        from agent.graph import _with_step_timeout

        fast = _with_step_timeout(lambda s: {"x": 1}, "fast", 5.0)
        assert fast({"errors": []})["x"] == 1

        def _boom(_state):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            _with_step_timeout(_boom, "bad", 5.0)({"errors": []})

        def _inner_timeout(_state):
            raise TimeoutError("the node's own timeout")

        with pytest.raises(TimeoutError, match="the node's own"):
            _with_step_timeout(_inner_timeout, "inner", 5.0)({"errors": []})

        def _slow(_state):
            time.sleep(0.3)
            return {}

        out = _with_step_timeout(_slow, "slow", 0.05)({"errors": []})
        assert out["node_failed"]["node"] == "slow"
        assert "step timeout" in out["node_failed"]["cause"]
        assert any("step timeout" in e for e in out["errors"])

    def test_step_timeout_disabled_runs_normally(self, sqlite_sessions, store):
        """step_timeout <= 0 disables the bound - the node runs inline."""
        deps, _caps = make_deps(
            sqlite_sessions,
            _llm([_plan("reapply_manifest", {"path": DEPLOYMENT_PATH})]),
            "cluster/crashloop",
            store,
            step_timeout=0,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)
        assert result.status == IncidentStatus.ESCALATED


class TestExecutionIdempotency:
    """One executions row per logical write (1.10): the unique
    idempotency_key plus the node's replay check mean a resumed or
    crashed run replays the recorded outcome instead of writing twice."""

    def test_duplicate_idempotency_key_rejected(self, sqlite_sessions, store):
        """The DB-level guarantee: a second row for the same
        (incident, fingerprint, attempt, kind) is an IntegrityError."""
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        verdict = decide("rerun_pipeline", {"job": "payment-api"}, "sandbox")
        with sqlite_sessions() as session:
            pk = session.scalar(
                select(models.Incident.id).where(models.Incident.incident_id == incident_id)
            )
            first_id = persist_execution(
                session,
                incident_pk=pk,
                attempt=0,
                verdict=verdict,
                approval=None,
                result=models.ExecutionResult.SUCCESS,
                detail="wrote it",
                output={"op": "trigger_build"},
                origin="fixture",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
            first = session.get(models.Execution, first_id)
            assert first.idempotency_key
            with pytest.raises(IntegrityError):
                with session.begin_nested():
                    persist_execution(
                        session,
                        incident_pk=pk,
                        attempt=0,
                        verdict=verdict,
                        approval=None,
                        result=models.ExecutionResult.SUCCESS,
                        detail="duplicated",
                        output={"op": "trigger_build"},
                        origin="fixture",
                        started_at=datetime.now(UTC),
                        finished_at=datetime.now(UTC),
                    )
            # A different attempt is a different logical write.
            second_id = persist_execution(
                session,
                incident_pk=pk,
                attempt=1,
                verdict=verdict,
                approval=None,
                result=models.ExecutionResult.SUCCESS,
                detail="next cycle",
                output={"op": "trigger_build"},
                origin="fixture",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
            assert session.get(models.Execution, second_id).idempotency_key != first.idempotency_key

    def test_execute_node_replays_existing_row(self, sqlite_sessions, store):
        """The node-level guarantee: if the executions row for this
        (fingerprint, attempt) already exists, the node replays its
        outcome and never calls the transport."""
        deps, caps = make_deps(sqlite_sessions, FakeLLM([]), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        params = {"path": DEPLOYMENT_PATH}
        fp = candidate_fingerprint("reapply_manifest", params)
        with sqlite_sessions() as session:
            pk = session.scalar(
                select(models.Incident.id).where(models.Incident.incident_id == incident_id)
            )
            verdict = decide("reapply_manifest", params, "sandbox")
            assert verdict.fingerprint == fp
            row_id = persist_execution(
                session,
                incident_pk=pk,
                attempt=0,
                verdict=verdict,
                approval=None,
                result=models.ExecutionResult.SUCCESS,
                detail="applied",
                output={"op": "apply_manifest"},
                origin="fixture",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
        node = execution_node(deps)
        out = node(
            {
                "incident_pk": pk,
                "incident_id": incident_id,
                "environment": "sandbox",
                "selected_action": {"action": "reapply_manifest", "params": params},
                "attempts": 0,
                "errors": [],
            }
        )
        assert out["execution"]["execution_id"] == row_id
        assert out["execution"]["result"] == "success"
        assert "idempotent replay" in out["execution"]["detail"]
        assert caps["kube_write"].calls == []
        assert len(_executions(sqlite_sessions, incident_id)) == 1

    def test_resume_at_executing_does_not_double_execute(self, sqlite_sessions, store):
        """The intake-side guarantee: a run resuming at `executing` with
        a persisted row reloads it and proceeds to verify - the
        remediation write is never called twice."""
        incident_id = _seed_executed(
            sqlite_sessions,
            K8S_SIGNAL,
            status=IncidentStatus.EXECUTING,
            action="reapply_manifest",
            params={"path": DEPLOYMENT_PATH},
            output={"op": "apply_manifest", "path": DEPLOYMENT_PATH},
        )
        deps, caps = make_deps(sqlite_sessions, _llm([]), "cluster/crashloop", store)
        result = run_incident(incident_id, deps=deps)

        # Exactly one transport call happened - the compensation apply,
        # not a re-execution of the remediation.
        assert caps["kube_write"].methods() == ["apply_manifest"]
        rows = _executions(sqlite_sessions, incident_id)
        assert len(rows) == 2  # seeded remediation + its compensation
        assert rows[1].compensates_id == rows[0].id
        assert result.status == IncidentStatus.ESCALATED

    def test_second_run_while_running_conflicts(self, sqlite_sessions, store):
        """The workflow_runs guard on SQLite: a `running` row makes the
        second start_run a RunConflictError."""
        from agent.runner import start_run

        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            run = start_run(session, incident)
            assert run.status == models.RunStatus.RUNNING
            with pytest.raises(RunConflictError):
                start_run(session, incident)


class TestDuplicateSignals:
    """Intake correlation under repeated signals (existing 1.2 rules,
    pinned at unit tier): the same fingerprint attaches to the open
    incident; only a terminal incident fingerprints open a new one."""

    def test_duplicate_signal_attaches_not_duplicates(self, sqlite_sessions):
        with sqlite_sessions() as session:
            first = intake_signal(session, K8S_SIGNAL)
            second = intake_signal(session, dict(K8S_SIGNAL))
            third = intake_signal(session, dict(K8S_SIGNAL))
            assert first.created is True
            assert second.created is False and third.created is False
            assert second.incident_id == first.incident_id
            signals = list(
                session.scalars(
                    select(models.Signal)
                    .join(models.Incident)
                    .where(models.Incident.incident_id == first.incident_id)
                )
            )
            assert len(signals) == 3

    def test_signal_after_terminal_opens_new_incident(self, sqlite_sessions):
        with sqlite_sessions() as session:
            first = intake_signal(session, K8S_SIGNAL)
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == first.incident_id)
            )
            for status in (
                IncidentStatus.DIAGNOSING,
                IncidentStatus.REMEDIATION_PENDING,
                IncidentStatus.ESCALATED,
            ):
                transition(incident, status, cause="t", session=session)
            second = intake_signal(session, dict(K8S_SIGNAL))
            assert second.created is True
            assert second.incident_id != first.incident_id


class TestEscalationProvenance:
    """Escalation is a first-class outcome: the cause rides the incident
    block and the compensation link rides the execution rows (1.10)."""

    def test_escalation_cause_in_provenance(self, sqlite_sessions, store):
        deps, _caps = make_deps(
            sqlite_sessions,
            _llm([CandidateList(candidates=[])]),  # empty plan -> escalate
            "cluster/crashloop",
            store,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        run_incident(incident_id, deps=deps)

        with sqlite_sessions() as session:
            record = assemble_provenance(session, incident_id)
        assert record["incident"]["status"] == "escalated"
        cause = record["incident"]["escalation_cause"]
        assert cause and "no candidate actions" in cause
        md = render_markdown(record)
        assert "escalation cause:" in md

    def test_compensation_link_in_provenance(self, sqlite_sessions, store):
        deps, _caps = make_deps(
            sqlite_sessions,
            _llm([_plan("reapply_manifest", {"path": DEPLOYMENT_PATH})]),
            "cluster/crashloop",
            store,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        run_incident(incident_id, deps=deps)

        with sqlite_sessions() as session:
            record = assemble_provenance(session, incident_id)
        execs = record["stages"]["execution"]
        assert len(execs) == 2
        assert execs[1]["compensates_id"] == execs[0]["id"]
        md = render_markdown(record)
        assert f"compensation for execution #{execs[0]['id']}" in md


@pytest.mark.integration
class TestRunConcurrencyPostgres:
    """The running-row guard under a real lock: two concurrent
    run_incident calls on the same incident - one wins, one conflicts;
    the write happens exactly once."""

    def test_concurrent_run_incident_single_execution(self, pg, pg_sessions, store):
        import threading

        class SlowKube(FakeKubernetesWrite):
            def apply_manifest(self, path):
                time.sleep(1.0)  # keep the winner's run row `running`
                return super().apply_manifest(path)

        from helix.config import get_settings

        deps, caps = make_deps(
            pg_sessions,
            _llm([_plan("reapply_manifest", {"path": DEPLOYMENT_PATH})]),
            "cluster/crashloop",
            store,
            database_url=get_settings().database_url,
        )
        deps = replace(deps, kubernetes_write=Recording(SlowKube()))
        incident_id = _make_incident(pg_sessions, K8S_SIGNAL)

        results: list = [None, None]
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def _worker(i):
            barrier.wait()
            try:
                results[i] = run_incident(incident_id, deps=deps)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        # The loser saw the `running` row; the winner's run terminated.
        assert len(errors) == 1
        assert isinstance(errors[0], RunConflictError)
        winner = next(r for r in results if r is not None)
        assert winner.status == IncidentStatus.ESCALATED
        # Exactly one remediation execution (+ its compensation) - never
        # two writes racing.
        with pg_sessions() as session:
            pk = session.scalar(
                select(models.Incident.id).where(models.Incident.incident_id == incident_id)
            )
            runs = list(
                session.scalars(
                    select(models.WorkflowRun).where(models.WorkflowRun.incident_id == pk)
                )
            )
            rows = list(
                session.scalars(select(models.Execution).where(models.Execution.incident_id == pk))
            )
        assert len(runs) == 1
        assert runs[0].status == models.RunStatus.TERMINAL
        assert len([r for r in rows if r.compensates_id is None]) == 1
