"""Subphase 1.8: the safety-policy gate, the executors, and the approval
lifecycle - all at unit tier (SQLite state, fixture evidence, scripted
FakeLLM, recording fake write transports).

Two enforcement layers are tested separately: the coordinator's routing
(a REQUIRE_APPROVAL verdict parks the run at awaiting_approval and no
write transport is called) and the executor's own gate (a gated action
driven straight at `executing` - e.g. after manual status tampering or a
routing bug - is still refused before the transport).

Fixture reachability note: only `reapply_manifest` (dry-run), `rerun_pipeline`
(job lookup) and `open_fix_pr` (content) can PASS validation over the
recorded fixtures - no fixture carries live deployment state, so
deployment-targeted actions fail validation honestly and never reach the
gate through the graph. The kube executor is therefore exercised directly
at unit level for its refusal paths.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import JENKINS_SIGNAL, K8S_SIGNAL, FakeLLM, Recording, make_deps
from pydantic import BaseModel
from sqlalchemy import select

from agent.actions.catalogue import ActionSpec, Reversibility, RiskTier, all_actions
from agent.agents.diagnosis import EvidencePlan
from agent.agents.planner import CandidateList, CandidateProposal
from agent.execution import (
    ExecutionContext,
    ExecutionOutcome,
    ExecutionRefusedError,
    enforce_execution_gate,
    executor_for,
)
from agent.runner import run_incident
from agent.state import Hypothesis
from agent.validation import candidate_fingerprint
from helix.cli import main as helix_main
from integrations.fakes import FakeKubernetesWrite
from integrations.writes import WriteError
from state import models
from state.intake import intake_signal
from state.machine import IncidentStatus

K8S = {"deployment": "payment-api", "namespace": "payment-api"}
DEPLOYMENT_PATH = "sandbox/payment-api/k8s/04-deployment.yaml"
CONFIGMAP_PATH = "sandbox/payment-api/k8s/02-configmap.yaml"
SECRET_PATH = "sandbox/payment-api/k8s/03-secret.yaml"
REPO_ROOT = Path(__file__).resolve().parents[2]


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


def _manifest_plan(path: str = DEPLOYMENT_PATH) -> CandidateList:
    return _plan("reapply_manifest", {"path": path}, "re-assert the committed manifest")


def _pipeline_plan() -> CandidateList:
    return _plan("rerun_pipeline", {"job": "payment-api"}, "re-run the failed job")


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


def _approvals(sessions, incident_id: str):
    return _rows(sessions, models.Approval, incident_id)


def _transitions(sessions, incident_id: str) -> list[tuple]:
    rows = _rows(sessions, models.Transition, incident_id)
    return [(r.from_status.value if r.from_status else None, r.to_status.value) for r in rows]


def _production_signal() -> dict:
    return {**K8S_SIGNAL, "environment": "production"}


class TestPolicyGateE2E:
    """The coordinator-side enforcement: verdict -> edge -> run shape."""

    def test_low_risk_sandbox_executes_through_fake(self, sqlite_sessions, store):
        """Low-tier write in non-prod -> ALLOW_WITH_CHECKS -> the fake
        jenkins write records the intended trigger and the run ends at
        verifying with a complete executions row."""
        deps, caps = make_deps(
            sqlite_sessions, _llm([_pipeline_plan()]), "ci/test-regression", store
        )
        incident_id = _make_incident(sqlite_sessions, JENKINS_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.error is None
        assert result.status == IncidentStatus.VERIFYING
        assert result.run_status == models.RunStatus.TERMINAL

        rows = _executions(sqlite_sessions, incident_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.action == "rerun_pipeline"
        assert row.policy_decision == "allow_with_checks"
        assert row.result == models.ExecutionResult.SUCCESS
        assert row.approval_id is None
        assert row.decided_by is None
        assert row.origin == "fixture"
        assert row.started_at is not None and row.finished_at is not None
        assert row.output["op"] == "trigger_build"
        assert row.output["recorded"] is True

        # The recording fake saw the intended write - no real Jenkins.
        assert caps["jenkins_write"].methods() == ["trigger_build"]
        assert caps["jenkins_write"].impl.calls[0]["job"] == "payment-api"

    def test_medium_risk_sandbox_executes_with_checks(self, sqlite_sessions, store):
        """Medium-tier write in non-prod -> ALLOW_WITH_CHECKS with the
        verification requirement recorded on the executions row."""
        deps, caps = make_deps(
            sqlite_sessions, _llm([_manifest_plan()]), "cluster/crashloop", store
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.VERIFYING
        rows = _executions(sqlite_sessions, incident_id)
        assert len(rows) == 1
        assert rows[0].action == "reapply_manifest"
        assert rows[0].policy_decision == "allow_with_checks"
        assert rows[0].verification_required is True
        assert rows[0].result == models.ExecutionResult.SUCCESS
        assert rows[0].output["op"] == "apply_manifest"
        assert rows[0].output["path"].endswith("04-deployment.yaml")
        assert caps["kube_write"].methods() == ["apply_manifest"]

    def test_production_write_halts_at_awaiting_approval(self, sqlite_sessions, store):
        """Layer 1 - routing: even a medium-tier write against production
        is REQUIRE_APPROVAL; the run suspends at awaiting_approval with a
        pending approvals row and NO write was attempted."""
        deps, caps = make_deps(
            sqlite_sessions, _llm([_manifest_plan()]), "cluster/crashloop", store
        )
        incident_id = _make_incident(sqlite_sessions, _production_signal())

        result = run_incident(incident_id, deps=deps)

        assert result.error is None
        assert result.status == IncidentStatus.AWAITING_APPROVAL
        assert result.run_status == models.RunStatus.SUSPENDED
        assert _transitions(sqlite_sessions, incident_id)[-1] == (
            "validating",
            "awaiting_approval",
        )

        approvals = _approvals(sqlite_sessions, incident_id)
        assert len(approvals) == 1
        approval = approvals[0]
        assert approval.status == models.ApprovalStatus.PENDING
        assert approval.action == "reapply_manifest"
        assert approval.decided_by is None
        assert approval.fingerprint == candidate_fingerprint(
            "reapply_manifest", {"path": DEPLOYMENT_PATH}
        )

        # Nothing was attempted - no executions row, no write call.
        assert _executions(sqlite_sessions, incident_id) == []
        assert caps["kube_write"].calls == []

    def test_secret_tier_requires_approval_even_in_sandbox(self, sqlite_sessions, store):
        """High-tier actions gate regardless of environment."""
        deps, caps = make_deps(
            sqlite_sessions,
            _llm(
                [_manifest_plan(SECRET_PATH)],
                claim="crashloop caused by a blank secret value",
            ),
            "cluster/crashloop",
            store,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.AWAITING_APPROVAL
        approvals = _approvals(sqlite_sessions, incident_id)
        assert len(approvals) == 1
        assert approvals[0].status == models.ApprovalStatus.PENDING
        assert caps["kube_write"].calls == []

    def test_pending_approval_rerun_stays_suspended(self, sqlite_sessions, store):
        """`helix run` while the approval is still pending ends suspended
        again - the gate holds until a decision exists."""
        deps, caps = make_deps(
            sqlite_sessions, _llm([_manifest_plan()]), "cluster/crashloop", store
        )
        incident_id = _make_incident(sqlite_sessions, _production_signal())
        assert run_incident(incident_id, deps=deps).status == IncidentStatus.AWAITING_APPROVAL

        second = run_incident(incident_id, deps=deps)

        assert second.status == IncidentStatus.AWAITING_APPROVAL
        assert second.run_status == models.RunStatus.SUSPENDED
        assert caps["kube_write"].calls == []
        # Still exactly one pending row - the gate never duplicates it.
        assert len(_approvals(sqlite_sessions, incident_id)) == 1


class _BogusParams(BaseModel):
    pass


def _ctx(sessions, incident_pk: int, environment: str, kube=None):
    """An ExecutionContext on a real session so the gate's approvals
    lookup runs against the store."""
    session_holder = sessions()
    session = session_holder.__enter__()
    return session_holder, ExecutionContext(
        session=session,
        incident_pk=incident_pk,
        incident_id="INC-test",
        environment=environment,
        kubernetes=kube or FakeKubernetesWrite(),
        github=None,
        jenkins=None,
        repo_root=REPO_ROOT,
        origin="fixture",
    )


def _incident_pk(sessions, incident_id: str) -> int:
    with sessions() as session:
        return session.scalar(
            select(models.Incident.id).where(models.Incident.incident_id == incident_id)
        )


class TestExecutorGate:
    """Layer 2 - enforcement inside the executor, independent of routing."""

    def test_require_approval_refused_without_granted_row(self, sqlite_sessions, store):
        """A gated action aimed straight at the executor - as if the
        coordinator had been bypassed - is still refused: no approvals
        row, no transport call."""
        from agent.actions.catalogue import get_action

        incident_id = _make_incident(sqlite_sessions, _production_signal())
        pk = _incident_pk(sqlite_sessions, incident_id)
        kube = FakeKubernetesWrite()
        spec = get_action("restart_deployment")
        params = spec.params_model.model_validate(K8S)
        holder, ctx = _ctx(sqlite_sessions, pk, "production", kube)

        with pytest.raises(ExecutionRefusedError, match="requires approval"):
            enforce_execution_gate(ctx, spec, params)
        holder.__exit__(None, None, None)
        assert kube.calls == []

    def test_executor_refuses_gated_action_directly(self, sqlite_sessions, store):
        """The family executor itself enforces the gate - calling the
        registered executor around the node is still refused."""
        from agent.actions.catalogue import get_action
        from agent.execution import kubernetes

        incident_id = _make_incident(sqlite_sessions, _production_signal())
        pk = _incident_pk(sqlite_sessions, incident_id)
        kube = FakeKubernetesWrite()
        spec = get_action("restart_deployment")
        params = spec.params_model.model_validate(K8S)
        holder, ctx = _ctx(sqlite_sessions, pk, "production", kube)

        with pytest.raises(ExecutionRefusedError):
            kubernetes.execute(ctx, spec, params)
        holder.__exit__(None, None, None)
        assert kube.calls == []

    def test_granted_row_for_different_fingerprint_does_not_bless(self, sqlite_sessions, store):
        """An approval recorded for one (action, params) can never bless
        another - the gate keys on the fingerprint."""
        from agent.actions.catalogue import get_action
        from agent.execution import kubernetes

        incident_id = _make_incident(sqlite_sessions, _production_signal())
        pk = _incident_pk(sqlite_sessions, incident_id)
        with sqlite_sessions() as session:
            session.add(
                models.Approval(
                    incident_id=pk,
                    action="scale_deployment",
                    params={**K8S, "replicas": 2},
                    fingerprint=candidate_fingerprint("scale_deployment", {**K8S, "replicas": 2}),
                    status=models.ApprovalStatus.APPROVED,
                    decided_by="alice",
                    decided_at=datetime.now(UTC),
                )
            )
        kube = FakeKubernetesWrite()
        spec = get_action("restart_deployment")
        params = spec.params_model.model_validate(K8S)
        holder, ctx = _ctx(sqlite_sessions, pk, "production", kube)

        with pytest.raises(ExecutionRefusedError):
            kubernetes.execute(ctx, spec, params)
        holder.__exit__(None, None, None)
        assert kube.calls == []

    def test_granted_row_allows_gated_execution(self, sqlite_sessions, store):
        """With a granted approvals row matching the fingerprint, the same
        gated action executes through the transport."""
        from agent.actions.catalogue import get_action
        from agent.execution import kubernetes

        incident_id = _make_incident(sqlite_sessions, _production_signal())
        pk = _incident_pk(sqlite_sessions, incident_id)
        with sqlite_sessions() as session:
            session.add(
                models.Approval(
                    incident_id=pk,
                    action="restart_deployment",
                    params=dict(K8S),
                    fingerprint=candidate_fingerprint("restart_deployment", K8S),
                    status=models.ApprovalStatus.APPROVED,
                    decided_by="alice",
                    decided_at=datetime.now(UTC),
                )
            )
        kube = FakeKubernetesWrite()
        spec = get_action("restart_deployment")
        params = spec.params_model.model_validate(K8S)
        holder, ctx = _ctx(sqlite_sessions, pk, "production", kube)

        outcome = kubernetes.execute(ctx, spec, params)
        holder.__exit__(None, None, None)

        assert isinstance(outcome, ExecutionOutcome)
        assert kube.calls[0]["op"] == "rollout_restart"

    def test_deny_refused_at_executor(self, sqlite_sessions, store):
        """A non-catalogue action is refused by the gate even when aimed
        at it directly - decide() says DENY before any transport."""
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        pk = _incident_pk(sqlite_sessions, incident_id)
        kube = FakeKubernetesWrite()
        bogus = ActionSpec(
            name="delete_everything",
            params_model=_BogusParams,
            risk_tier=lambda p: RiskTier.LOW,
            preconditions=(),
            expected_effect="none",
            reversibility=Reversibility.STATELESS,
            compensating_action=None,
            validator="none",
            executor="kubernetes",
            blast_radius=1.0,
            params_hint="",
        )
        holder, ctx = _ctx(sqlite_sessions, pk, "sandbox", kube)

        with pytest.raises(ExecutionRefusedError, match="policy denied"):
            enforce_execution_gate(ctx, bogus, _BogusParams())
        holder.__exit__(None, None, None)
        assert kube.calls == []

    def test_bypassed_coordinator_still_refused_end_to_end(self, sqlite_sessions, store):
        """The full second-layer path: park the incident directly at
        `executing` (simulating a routing bug / manual tamper) with a
        production-gated selection and no approvals row. The run must
        refuse, audit `refused`, and never call the transport."""
        from state.machine import transition

        deps, caps = make_deps(
            sqlite_sessions, _llm([_manifest_plan()]), "cluster/crashloop", store
        )
        incident_id = _make_incident(sqlite_sessions, _production_signal())
        # Seed a selected candidate + jump the incident to executing -
        # the coordinator's gate never ran.
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            session.add(
                models.Candidate(
                    incident_id=incident.id,
                    attempt=0,
                    rank=1,
                    action="reapply_manifest",
                    params={"path": DEPLOYMENT_PATH},
                    rationale="seeded",
                    evidence_refs=[],
                    score=1.0,
                    risk_tier="medium",
                    selected=True,
                    components={},
                )
            )
            transition(incident, IncidentStatus.DIAGNOSING, cause="t", session=session)
            transition(incident, IncidentStatus.REMEDIATION_PENDING, cause="t", session=session)
            transition(incident, IncidentStatus.VALIDATING, cause="t", session=session)
            transition(incident, IncidentStatus.EXECUTING, cause="t", session=session)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.FAILED
        assert caps["kube_write"].calls == []
        rows = _executions(sqlite_sessions, incident_id)
        assert len(rows) == 1
        assert rows[0].result == models.ExecutionResult.REFUSED
        assert "requires approval" in rows[0].detail
        assert _transitions(sqlite_sessions, incident_id)[-1] == ("executing", "failed")

    def test_every_catalogue_executor_key_resolves(self):
        """Registry completeness: every catalogue action's executor key
        maps to a real executor - a gap would be a silent dead end."""
        for spec in all_actions():
            assert executor_for(spec) is not None


class TestApprovalLifecycle:
    """approve / reject through the real CLI + runner."""

    def _suspend_on_gate(self, sqlite_sessions, store, plans, signal=None, claim=None):
        llm = _llm(plans, claim=claim or "payment-api is wedged")
        deps, caps = make_deps(sqlite_sessions, llm, "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, signal or _production_signal())
        result = run_incident(incident_id, deps=deps)
        assert result.status == IncidentStatus.AWAITING_APPROVAL
        return deps, caps, incident_id

    def _patch_cli_db(self, monkeypatch, sqlite_sessions):
        monkeypatch.setattr("state.db.session_scope", lambda *a, **kw: sqlite_sessions())

    def test_approve_records_actor_and_executes(self, sqlite_sessions, store, monkeypatch, capsys):
        deps, caps, incident_id = self._suspend_on_gate(sqlite_sessions, store, [_manifest_plan()])
        self._patch_cli_db(monkeypatch, sqlite_sessions)
        monkeypatch.setattr(
            "agent.runner.run_incident", lambda iid, **kw: run_incident(iid, deps=deps)
        )

        rc = helix_main(["approve", incident_id, "--actor", "alice"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "approved by alice" in out

        approvals = _approvals(sqlite_sessions, incident_id)
        assert len(approvals) == 1
        approval = approvals[0]
        assert approval.status == models.ApprovalStatus.APPROVED
        assert approval.decided_by == "alice"
        assert approval.decided_at is not None

        # The resumed run executed through the fake and reached verifying.
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert incident.status == IncidentStatus.VERIFYING
        assert _transitions(sqlite_sessions, incident_id)[-2:] == [
            ("awaiting_approval", "executing"),
            ("executing", "verifying"),
        ]
        rows = _executions(sqlite_sessions, incident_id)
        assert len(rows) == 1
        assert rows[0].result == models.ExecutionResult.SUCCESS
        assert rows[0].approval_id == approval.id
        assert rows[0].decided_by == "alice"
        assert rows[0].policy_decision == "require_approval"
        assert caps["kube_write"].methods() == ["apply_manifest"]

    def test_approve_uses_helix_operator_by_default(
        self, sqlite_sessions, store, monkeypatch, capsys
    ):
        deps, _caps, incident_id = self._suspend_on_gate(sqlite_sessions, store, [_manifest_plan()])
        self._patch_cli_db(monkeypatch, sqlite_sessions)
        monkeypatch.setenv("HELIX_OPERATOR", "oncall-bot")
        monkeypatch.setattr(
            "agent.runner.run_incident", lambda iid, **kw: run_incident(iid, deps=deps)
        )

        assert helix_main(["approve", incident_id]) == 0
        capsys.readouterr()
        approvals = _approvals(sqlite_sessions, incident_id)
        assert approvals[0].decided_by == "oncall-bot"

    def test_reject_replans_with_exclusion(self, sqlite_sessions, store, monkeypatch, capsys):
        """Rejected candidate joins the exclusion list: the resumed run
        replans, the ranker drops the re-proposed rejected action, and the
        fallback executes instead."""
        gated = _manifest_plan(SECRET_PATH)
        # The replanned proposal set deliberately re-proposes the rejected
        # candidate: the ranker must drop it via the exclusion list.
        fallback = CandidateList(
            candidates=[
                CandidateProposal(
                    action="reapply_manifest",
                    params={"path": SECRET_PATH},
                    rationale="try the secret again",
                ),
                CandidateProposal(
                    action="reapply_manifest",
                    params={"path": CONFIGMAP_PATH},
                    rationale="re-assert the configmap instead",
                ),
            ]
        )
        deps, caps, incident_id = self._suspend_on_gate(
            sqlite_sessions,
            store,
            [gated, fallback],
            signal=K8S_SIGNAL,
            claim="stale secret and env config drifted",
        )
        self._patch_cli_db(monkeypatch, sqlite_sessions)
        monkeypatch.setattr(
            "agent.runner.run_incident", lambda iid, **kw: run_incident(iid, deps=deps)
        )

        rc = helix_main(["reject", incident_id, "--actor", "bob", "--note", "too risky"])

        assert rc == 0
        approvals = _approvals(sqlite_sessions, incident_id)
        assert approvals[0].status == models.ApprovalStatus.REJECTED
        assert approvals[0].decided_by == "bob"
        assert approvals[0].note == "too risky"

        # The replan executed the non-excluded fallback.
        rows = _executions(sqlite_sessions, incident_id)
        assert len(rows) == 1
        assert rows[0].action == "reapply_manifest"
        assert rows[0].params["path"] == CONFIGMAP_PATH
        assert rows[0].result == models.ExecutionResult.SUCCESS
        assert caps["kube_write"].methods() == ["apply_manifest"]
        # The rejected fingerprint was dropped by the ranker - its drop
        # reason surfaced as a run note - and can never be re-proposed.
        rejected_fp = candidate_fingerprint("reapply_manifest", {"path": SECRET_PATH})
        assert all(r.fingerprint != rejected_fp for r in rows)
        err = capsys.readouterr().err
        assert "identical params already" in err
        # And the incident replanned rather than ending.
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert incident.status == IncidentStatus.VERIFYING
            assert (
                "awaiting_approval",
                "remediation_pending",
            ) in _transitions(sqlite_sessions, incident_id)

    def test_duplicate_approve_is_a_noop_not_double_execution(
        self, sqlite_sessions, store, monkeypatch, capsys
    ):
        deps, caps, incident_id = self._suspend_on_gate(sqlite_sessions, store, [_manifest_plan()])
        self._patch_cli_db(monkeypatch, sqlite_sessions)
        monkeypatch.setattr(
            "agent.runner.run_incident", lambda iid, **kw: run_incident(iid, deps=deps)
        )

        assert helix_main(["approve", incident_id, "--actor", "alice"]) == 0
        assert helix_main(["approve", incident_id, "--actor", "alice"]) == 0
        out = capsys.readouterr().out
        assert "already approved" in out

        # Exactly one execution happened - the decided row cannot re-fire.
        rows = _executions(sqlite_sessions, incident_id)
        assert len(rows) == 1
        assert caps["kube_write"].methods() == ["apply_manifest"]

    def test_approve_unknown_incident_is_clean_error(
        self, sqlite_sessions, store, monkeypatch, capsys
    ):
        self._patch_cli_db(monkeypatch, sqlite_sessions)
        rc = helix_main(["approve", "INC-nope"])
        assert rc == 2
        assert "no such incident" in capsys.readouterr().err

    def test_approve_with_nothing_pending_is_clean_error(
        self, sqlite_sessions, store, monkeypatch, capsys
    ):
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        self._patch_cli_db(monkeypatch, sqlite_sessions)
        rc = helix_main(["approve", incident_id])
        assert rc == 2
        assert "no pending approval" in capsys.readouterr().err

    def test_approvals_lists_pending_rows(self, sqlite_sessions, store, monkeypatch, capsys):
        self._suspend_on_gate(sqlite_sessions, store, [_manifest_plan()])
        self._patch_cli_db(monkeypatch, sqlite_sessions)

        assert helix_main(["approvals"]) == 0
        out = capsys.readouterr().out
        assert "restart" not in out  # sanity: ours is the manifest plan
        assert "reapply_manifest" in out
        assert "pending" in out


class TestExecutionFailure:
    def test_transport_error_fails_run_and_excludes(self, sqlite_sessions, store):
        """A write transport failure -> executions row `error`, incident
        -> failed, and the fingerprint joins the exclusion list so a
        rediagnosed plan cannot blindly repeat it."""
        from dataclasses import replace

        class DownKube(FakeKubernetesWrite):
            def apply_manifest(self, path):
                raise WriteError("cluster unreachable")

        deps, _ = make_deps(sqlite_sessions, _llm([_manifest_plan()]), "cluster/crashloop", store)
        deps = replace(deps, kubernetes_write=Recording(DownKube()))
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.FAILED
        rows = _executions(sqlite_sessions, incident_id)
        assert len(rows) == 1
        assert rows[0].result == models.ExecutionResult.ERROR
        assert "cluster unreachable" in rows[0].detail
        assert rows[0].finished_at is not None
        assert _transitions(sqlite_sessions, incident_id)[-1] == ("executing", "failed")

        # The failed fingerprint is in the persisted exclusion set.
        from agent.graph import _excluded_fingerprints

        with sqlite_sessions() as session:
            pk = session.scalar(
                select(models.Incident.id).where(models.Incident.incident_id == incident_id)
            )
            excluded = _excluded_fingerprints(session, pk)
        fp = candidate_fingerprint("reapply_manifest", {"path": DEPLOYMENT_PATH})
        assert fp in excluded
