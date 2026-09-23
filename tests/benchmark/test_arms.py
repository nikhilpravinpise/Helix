"""Ablation arms (subphase 1.11, D8): arm A's deterministic rule tables
and arm B's fused node, exercised through the real graph on fixture-mode
deps - no live infrastructure.

Arm A must make ZERO LLM calls: `llm.calls` staying empty is the honest
assertion, not a mock of the outcome. Arm B's fused node gets a scripted
FusedDiagnosisPlan; its runs are multi-hop (no checkpointer on SQLite) and
end parked at `verifying` - the designed no-verify end state.
"""

from fakes import FakeLLM, make_deps
from sqlalchemy import select

from agent.agents.fused import FusedDiagnosisPlan
from agent.runner import run_incident
from benchmark.approver import service_suspended_run
from state import models
from state.intake import intake_signal
from state.machine import IncidentStatus

JENKINS_SIGNAL = {
    "source": "jenkins",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "high",
    "signature": {"job": "payment-api", "stage": "Test"},
    "raw": {"job": "payment-api", "build_number": 2, "result": "FAILURE"},
}

GITHUB_PR_SIGNAL = {
    "source": "github",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "medium",
    "signature": {
        "type": "pull_request_review",
        "pr": 17,
        "review_state": "CHANGES_REQUESTED",
        "branch": "feature/payment-config",
    },
    "raw": {
        "pr": 17,
        "branch": "feature/payment-config",
        "commit": "9f17aa2d4b0c8d1e2f3a4b5c6d7e8f9a0b1c2d3e",
        "base": "main",
    },
}

GITHUB_CHECK_SIGNAL = {
    "source": "github",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "medium",
    "signature": {
        "type": "check_run",
        "check": "payment-api-ci",
        "conclusion": "failure",
        "ref": "77aa01c9e2b3f4d5a6b7c8d9e0f1a2b3c4d5e6f7",
    },
    "raw": {"commit": "77aa01c9e2b3f4d5a6b7c8d9e0f1a2b3c4d5e6f7", "jenkins_job": "payment-api"},
}

GITHUB_BRANCH_SIGNAL = {
    "source": "github",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "medium",
    "signature": {
        "type": "branch_state",
        "branch": "feature/timeout-tuning",
        "comparison": "diverged",
        "behind_by": 14,
    },
    "raw": {"branch": "feature/timeout-tuning", "base": "main"},
}


def _intake(sessions, signal: dict) -> str:
    with sessions() as session:
        return intake_signal(session, signal).incident_id


def _status(sessions, incident_id: str) -> str:
    with sessions() as session:
        return session.scalar(
            select(models.Incident.status).where(models.Incident.incident_id == incident_id)
        )


def _drive_b(incident_id, deps, hops=8):
    """Arm B needs several `helix run` equivalents: the fused node leaves
    the run at remediation_pending, the policy/execute hop follows, then
    it parks at verifying (no verify stage)."""
    last = None
    for _ in range(hops):
        res = run_incident(incident_id, deps=deps, arm="B")
        if res.error:
            return res
        if str(res.status) == str(last):
            break
        last = res.status
    return res


class TestArmARules:
    def test_ci_fixture_recovers_with_zero_llm_calls(self, sqlite_sessions, store):
        """Arm A on the test-regression fixture: rules select open_fix_pr,
        the shared validate/policy/execute/verify path runs it, and the
        LLM is never consulted."""
        llm = FakeLLM()
        deps, caps = make_deps(sqlite_sessions, llm, "ci/test-regression", store)
        iid = _intake(sqlite_sessions, JENKINS_SIGNAL)

        res = run_incident(iid, deps=deps, arm="A")

        assert res.error is None
        assert res.status == IncidentStatus.RECOVERED
        assert res.selected_action["action"] == "open_fix_pr"
        assert not llm.calls, "arm A must make zero LLM calls"
        assert caps["github_write"].methods() == ["open_fix_pr"]

    def test_rejected_pr_fixture_recovers(self, sqlite_sessions, store):
        deps, caps = make_deps(sqlite_sessions, FakeLLM(), "github/rejected-pr", store)
        iid = _intake(sqlite_sessions, GITHUB_PR_SIGNAL)

        res = run_incident(iid, deps=deps, arm="A")

        assert res.error is None
        assert res.status == IncidentStatus.RECOVERED
        assert res.selected_action["action"] == "open_fix_pr"

    def test_failed_check_fixture_recovers_via_rerun(self, sqlite_sessions, store):
        """The failed-check fixture's jenkins_result=SUCCESS is the world
        after a transient check re-ran - rerun_pipeline verifies green."""
        deps, caps = make_deps(sqlite_sessions, FakeLLM(), "github/failed-check", store)
        iid = _intake(sqlite_sessions, GITHUB_CHECK_SIGNAL)

        res = run_incident(iid, deps=deps, arm="A")

        assert res.error is None
        assert res.status == IncidentStatus.RECOVERED
        assert res.selected_action["action"] == "rerun_pipeline"
        assert caps["jenkins_write"].methods() == ["trigger_build"]

    def test_stale_branch_fixture_recovers(self, sqlite_sessions, store):
        deps, _caps = make_deps(sqlite_sessions, FakeLLM(), "github/stale-branch", store)
        iid = _intake(sqlite_sessions, GITHUB_BRANCH_SIGNAL)

        res = run_incident(iid, deps=deps, arm="A")

        assert res.error is None
        assert res.status == IncidentStatus.RECOVERED
        assert res.selected_action["action"] == "open_fix_pr"

    def test_gated_path_approval_then_verify_fail_then_replan(self, sqlite_sessions, store):
        """The full arm-A gated loop: rules select reapply_manifest on the
        Secret, policy gates it (high tier), the scripted approver (D2)
        approves the declared optimal, the write executes - and the frozen
        fixture's verification fails honestly (pods never recover on
        recorded writes). Compensation runs, the replan proposes
        open_fix_pr on the Secret file - ALSO high-tier - and D2's
        approve-only-the-optimal rule declines it, so the incident
        escalates. Every stage real, every row audited."""
        missing_secret = {
            "source": "kubernetes",
            "service": "payment-api",
            "environment": "sandbox",
            "severity": "high",
            "signature": {
                "waiting_reason": "CreateContainerConfigError",
                "container": "payment-api",
            },
            "raw": {"namespace": "payment-api"},
        }
        scenario = {
            "name": "missing-secret",
            "approval_gated": True,
            "optimal_action": "reapply_manifest",
        }
        deps, caps = make_deps(sqlite_sessions, FakeLLM(), "cluster/missing-secret", store)
        iid = _intake(sqlite_sessions, missing_secret)

        res = run_incident(iid, deps=deps, arm="A")
        assert res.status == IncidentStatus.AWAITING_APPROVAL
        assert caps["kube_write"].methods() == []  # nothing executed pre-approval

        outcome = service_suspended_run(sqlite_sessions, iid, scenario)
        assert outcome.outcome == "approved"

        res = run_incident(iid, deps=deps, arm="A")
        # Executed the gated write, verified honestly-failing on the frozen
        # fixture, compensated, replanned to a second gated action.
        assert res.status == IncidentStatus.AWAITING_APPROVAL
        outcome = service_suspended_run(sqlite_sessions, iid, scenario)
        # D2: open_fix_pr is not the declared optimal -> rejected.
        assert outcome.outcome == "rejected"

        res = run_incident(iid, deps=deps, arm="A")
        assert res.status == IncidentStatus.ESCALATED
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == iid)
            )
            actions = [e.action for e in incident.executions]
            assert "reapply_manifest" in actions  # the gated write fired post-approval
            assert any(
                e.compensates_id is not None for e in incident.executions
            )  # compensation ran
            statuses = {a.status for a in incident.approvals}
            assert models.ApprovalStatus.APPROVED in statuses
            assert models.ApprovalStatus.REJECTED in statuses
            assert all(a.decided_by == "benchmark-approver" for a in incident.approvals)

    def test_unmatched_signature_escalates_honestly(self, sqlite_sessions, store):
        """A signature the table does not cover escalates - the rules arm
        never invents a diagnosis."""
        deps, _caps = make_deps(sqlite_sessions, FakeLLM(), "ci/test-regression", store)
        iid = _intake(
            sqlite_sessions,
            {
                **JENKINS_SIGNAL,
                "signature": {"job": "payment-api", "stage": "Deploy to prod"},
            },
        )
        res = run_incident(iid, deps=deps, arm="A")
        assert res.error is None
        assert res.status == IncidentStatus.ESCALATED


def _drive_c(incident_id, deps, hops=8):
    """Arm C shares B's composition exactly (E6: the fused node plus
    retrieval context) - same multi-hop shape as `_drive_b`."""
    last = None
    for _ in range(hops):
        res = run_incident(incident_id, deps=deps, arm="C")
        if res.error:
            return res
        if str(res.status) == str(last):
            break
        last = res.status
    return res


class TestArmCFused:
    """E6: arm C is arm B's fused node plus retrieval context - identical
    graph composition. Regression coverage for a routing bug where the
    coordinator's per-arm dicts and arm-B special cases (`make_router`,
    `_decide`, `_policy_gate`, `_intake_node`'s plan_complete reload) were
    never extended to "C": `build_graph(arm="C")` raised `KeyError: 'C'`
    on every call, and even after that no incident could progress past
    `remediation_pending` because plan_complete was never reloaded for a
    resumed arm-C run. No test built the arm-C graph or drove it before
    this, so nothing caught it."""

    def test_fused_node_executes_through_the_shared_gate(self, sqlite_sessions, store):
        answer = FusedDiagnosisPlan(
            claim="broken test assertion in tests/test_main.py",
            confidence=0.8,
            action="open_fix_pr",
            params={
                "title": "Fix the assertion",
                "body": "Corrected by arm C fused call",
                "branch": "helix/INC-test",
                "files": [{"path": "sandbox/payment-api/tests/test_main.py", "content": "x=1\n"}],
            },
            rationale="the test asserts 999",
        )
        deps, caps = make_deps(sqlite_sessions, FakeLLM([answer]), "ci/test-regression", store)
        iid = _intake(sqlite_sessions, JENKINS_SIGNAL)

        res = _drive_c(iid, deps)

        assert res.error is None
        assert _status(sqlite_sessions, iid) == "verifying"  # parked - no verify stage
        assert caps["github_write"].methods() == ["open_fix_pr"]
        assert [c for c in deps.llm.calls if c[0] == "structured"] == [
            ("structured", FusedDiagnosisPlan)
        ]

    def test_fused_provenance_records_arm_c_not_b(self, sqlite_sessions, store):
        """The persisted candidate's `components["arm"]` must say which
        arm actually ran - hardcoding "B" here would mislabel arm C."""
        answer = FusedDiagnosisPlan(
            claim="transient check failure",
            confidence=0.9,
            action="rerun_pipeline",
            params={"job": "payment-api"},
        )
        deps, _caps = make_deps(sqlite_sessions, FakeLLM([answer]), "github/failed-check", store)
        iid = _intake(sqlite_sessions, GITHUB_CHECK_SIGNAL)

        run_incident(iid, deps=deps, arm="C")

        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == iid)
            )
            selected = [c for c in incident.candidates if c.selected]
            assert len(selected) == 1
            assert selected[0].components["arm"] == "C"


class TestArmBFused:
    def test_fused_node_executes_through_the_shared_gate(self, sqlite_sessions, store):
        """A scripted fused answer lands hypothesis + candidate, the shared
        policy gate + executor run it, and the run parks at verifying
        (no verify stage - the designed arm-B end state)."""
        answer = FusedDiagnosisPlan(
            claim="broken test assertion in tests/test_main.py",
            confidence=0.8,
            action="open_fix_pr",
            params={
                "title": "Fix the assertion",
                "body": "Corrected by arm B fused call",
                "branch": "helix/INC-test",
                "files": [{"path": "sandbox/payment-api/tests/test_main.py", "content": "x=1\n"}],
            },
            rationale="the test asserts 999",
        )
        deps, caps = make_deps(sqlite_sessions, FakeLLM([answer]), "ci/test-regression", store)
        iid = _intake(sqlite_sessions, JENKINS_SIGNAL)

        res = _drive_b(iid, deps)

        assert res.error is None
        assert _status(sqlite_sessions, iid) == "verifying"  # parked - no verify stage
        assert caps["github_write"].methods() == ["open_fix_pr"]
        # One fused LLM call total - no evidence-request rounds, no replan.
        assert [c for c in deps.llm.calls if c[0] == "structured"] == [
            ("structured", FusedDiagnosisPlan)
        ]

    def test_fused_empty_action_escalates(self, sqlite_sessions, store):
        answer = FusedDiagnosisPlan(claim="nothing safe to do", confidence=0.3)
        deps, caps = make_deps(sqlite_sessions, FakeLLM([answer]), "ci/test-regression", store)
        iid = _intake(sqlite_sessions, JENKINS_SIGNAL)

        _drive_b(iid, deps)

        assert _status(sqlite_sessions, iid) == "escalated"
        assert caps["github_write"].methods() == []

    def test_fused_invalid_params_escalate(self, sqlite_sessions, store):
        """No validate node means an LLM's malformed params are caught by
        the catalogue bounds check inside the fused node - honest
        escalation, not a broken write."""
        answer = FusedDiagnosisPlan(
            claim="x",
            confidence=0.9,
            action="scale_deployment",
            params={"deployment": "payment-api", "replicas": -5},  # out of bounds
        )
        deps, caps = make_deps(sqlite_sessions, FakeLLM([answer]), "ci/test-regression", store)
        iid = _intake(sqlite_sessions, JENKINS_SIGNAL)

        _drive_b(iid, deps)

        assert _status(sqlite_sessions, iid) == "escalated"
        assert caps["kube_write"].methods() == []

    def test_fused_provenance_persists(self, sqlite_sessions, store):
        """The fused node writes hypothesis + selected candidate rows - the
        provenance timeline exists for arm B too."""
        answer = FusedDiagnosisPlan(
            claim="transient check failure",
            confidence=0.9,
            action="rerun_pipeline",
            params={"job": "payment-api"},
        )
        deps, _caps = make_deps(sqlite_sessions, FakeLLM([answer]), "github/failed-check", store)
        iid = _intake(sqlite_sessions, GITHUB_CHECK_SIGNAL)

        run_incident(iid, deps=deps, arm="B")

        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == iid)
            )
            assert incident.hypotheses[-1].claim == "transient check failure"
            selected = [c for c in incident.candidates if c.selected]
            assert len(selected) == 1
            assert selected[0].action == "rerun_pipeline"
            assert selected[0].components["arm"] == "B"

    def test_arm_b_rejected_approval_escalates_no_replan(self, sqlite_sessions, store):
        """A gated arm-B action suspends at awaiting_approval like every
        arm; when the approver rejects it, arm B escalates - it has no
        replan stage to fall back to (the ablation's point)."""
        answer = FusedDiagnosisPlan(
            claim="secret value is blank",
            confidence=0.9,
            action="reapply_manifest",
            params={"path": "sandbox/payment-api/k8s/03-secret.yaml"},
        )
        deps, _caps = make_deps(sqlite_sessions, FakeLLM([answer]), "cluster/missing-secret", store)
        iid = _intake(
            sqlite_sessions,
            {
                "source": "kubernetes",
                "service": "payment-api",
                "environment": "sandbox",
                "severity": "high",
                "signature": {"waiting_reason": "CreateContainerConfigError"},
                "raw": {"namespace": "payment-api"},
            },
        )
        for _ in range(6):
            res = run_incident(iid, deps=deps, arm="B")
            if str(res.status) == "awaiting_approval":
                break
        assert _status(sqlite_sessions, iid) == "awaiting_approval"

        outcome = service_suspended_run(
            sqlite_sessions,
            iid,
            # Not the scenario's optimal -> D2 rejects.
            {"name": "missing-secret", "approval_gated": True, "optimal_action": "open_fix_pr"},
        )
        assert outcome.outcome == "rejected"

        run_incident(iid, deps=deps, arm="B")
        assert _status(sqlite_sessions, iid) == "escalated"


def _drive_d(incident_id, deps, hops=8):
    """Arm D shares B/C's fused-node composition (2.8: same node, fed the
    composed event+graph knowledge context) - same multi-hop shape."""
    last = None
    for _ in range(hops):
        res = run_incident(incident_id, deps=deps, arm="D")
        if res.error:
            return res
        if str(res.status) == str(last):
            break
        last = res.status
    return res


class TestArmDKnowledge:
    """2.8: arm D is the fused node fed the composed GraphRAG context.
    The harness CLI accepts `--arm D`; this proves the graph side works
    end to end on the shared in-process deps."""

    def test_arm_d_executes_through_the_shared_gate(self, sqlite_sessions, store):
        answer = FusedDiagnosisPlan(
            claim="transient check failure",
            confidence=0.9,
            action="rerun_pipeline",
            params={"job": "payment-api"},
        )
        deps, caps = make_deps(sqlite_sessions, FakeLLM([answer]), "github/failed-check", store)
        iid = _intake(sqlite_sessions, GITHUB_CHECK_SIGNAL)

        res = _drive_d(iid, deps)

        assert res.error is None
        assert _status(sqlite_sessions, iid) == "verifying"  # parked like B/C
        assert caps["jenkins_write"].methods() == ["trigger_build"]
        assert [c for c in deps.llm.calls if c[0] == "structured"] == [
            ("structured", FusedDiagnosisPlan)
        ]

    def test_the_benchmark_accepts_arm_d(self):
        from benchmark.tolerance import BANDS

        assert "D" in BANDS

    def test_fused_provenance_records_arm_d(self, sqlite_sessions, store):
        answer = FusedDiagnosisPlan(
            claim="transient check failure",
            confidence=0.9,
            action="rerun_pipeline",
            params={"job": "payment-api"},
        )
        deps, _caps = make_deps(sqlite_sessions, FakeLLM([answer]), "github/failed-check", store)
        iid = _intake(sqlite_sessions, GITHUB_CHECK_SIGNAL)

        run_incident(iid, deps=deps, arm="D")

        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == iid)
            )
            selected = [c for c in incident.candidates if c.selected]
            assert len(selected) == 1
            assert selected[0].components["arm"] == "D"


class TestArmBParks:
    def test_verifying_is_stable_end_state(self, sqlite_sessions, store):
        """Once arm B parks at verifying, further `helix run` calls leave it
        there - no node exists to move it, and it never re-executes."""
        answer = FusedDiagnosisPlan(
            claim="x",
            confidence=0.9,
            action="rerun_pipeline",
            params={"job": "payment-api"},
        )
        deps, caps = make_deps(sqlite_sessions, FakeLLM([answer]), "github/failed-check", store)
        iid = _intake(sqlite_sessions, GITHUB_CHECK_SIGNAL)
        _drive_b(iid, deps)
        assert _status(sqlite_sessions, iid) == "verifying"

        run_incident(iid, deps=deps, arm="B")
        run_incident(iid, deps=deps, arm="B")

        assert _status(sqlite_sessions, iid) == "verifying"
        # The write fired exactly once across all four runs.
        assert caps["jenkins_write"].methods() == ["trigger_build"]
