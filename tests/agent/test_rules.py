"""Tests for agent/rules.py: deterministic rule matching and action generation.

Tests verify that K8S target params use the signal's service and namespace,
not hardcoded values; manifest paths are only generated for payment-api;
and existing payment-api rules still produce the same results.
"""

from fakes import FakeLLM, make_deps
from sqlalchemy import select

from agent.rules import match_rule, rules_plan_node
from agent.state import Hypothesis, WorkflowState
from integrations.evidence import Evidence, EvidenceKind, EvidenceOrigin, EvidenceSource
from state import models
from state.intake import intake_signal

K8S_SIGNAL_PAYMENT_API = {
    "source": "kubernetes",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "high",
    "signature": {"waiting_reason": "CrashLoopBackOff", "terminated_reason": "Error"},
    "raw": {"pod": "payment-api-98867c74d-h6j8v", "namespace": "payment-api"},
}

K8S_SIGNAL_BILLING_WORKER = {
    "source": "kubernetes",
    "service": "billing-worker",
    "environment": "sandbox",
    "severity": "high",
    "signature": {"waiting_reason": "CrashLoopBackOff", "terminated_reason": "Error"},
    "raw": {"pod": "billing-worker-abcd1234-xyz", "namespace": "billing-worker"},
}


def _evidence(*ids: str) -> list[Evidence]:
    """Synthetic collected evidence with explicit ids."""
    out = []
    for ev_id in ids:
        out.append(
            Evidence(
                id=ev_id,
                source=EvidenceSource.KUBERNETES,
                kind=EvidenceKind.POD_LIST,
                service="payment-api",
                summary=f"evidence {ev_id}",
                payload={"note": ev_id},
                origin=EvidenceOrigin.FIXTURE,
            )
        )
    return out


def _make_incident(sessions, signal: dict) -> str:
    with sessions() as session:
        return intake_signal(session, signal).incident_id


def _run_plan(
    deps,
    sessions,
    incident_id: str,
    *,
    hypothesis: Hypothesis,
    evidence: list[Evidence],
    signal: dict,
) -> dict:
    """Run the rules plan node on the given state."""
    with sessions() as session:
        incident = session.scalar(
            select(models.Incident).where(models.Incident.incident_id == incident_id)
        )
        state: WorkflowState = {
            "incident_id": incident_id,
            "incident_pk": incident.id,
            "service": incident.service,
            "environment": incident.environment,
            "severity": incident.severity,
            "status": "remediation_pending",
            "signal": {
                "source": signal["source"],
                "signature": signal.get("signature", {}),
                "raw": signal.get("raw", {}),
            },
            "evidence": [e.model_dump(mode="json") for e in evidence],
            "hypothesis": hypothesis.model_dump(mode="json"),
            "attempts": 0,
            "errors": [],
        }
    return rules_plan_node(deps)(state)


class TestRuleMatching:
    """Test that signals match the correct rules."""

    def test_payment_api_crashloop_matches_crashloop_rule(self):
        """Verify payment-api CrashLoopBackOff matches the crashloop rule."""
        rule = match_rule(K8S_SIGNAL_PAYMENT_API)
        assert rule is not None
        assert rule.fault == "crashloop"

    def test_billing_worker_crashloop_matches_crashloop_rule(self):
        """Verify billing-worker CrashLoopBackOff also matches the crashloop rule.

        This ensures that the rule's signature matching (waiting_reason,
        terminated_reason) is independent of service name."""
        rule = match_rule(K8S_SIGNAL_BILLING_WORKER)
        assert rule is not None
        assert rule.fault == "crashloop"


class TestK8sTargeting:
    """Test that K8S actions target the correct service and namespace."""

    def test_payment_api_crashloop_produces_payment_api_target(self, sqlite_sessions, store):
        """Existing behavior: payment-api signal produces payment-api targets."""
        deps, _ = make_deps(
            sqlite_sessions,
            FakeLLM(),
            store.load("crashloop"),
            store,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL_PAYMENT_API)
        evidence = _evidence("ev_crashloop_1")
        hypothesis = Hypothesis(
            claim="secret missing", evidence_refs=["ev_crashloop_1"], confidence=0.9
        )
        plan = _run_plan(
            deps,
            sqlite_sessions,
            incident_id,
            hypothesis=hypothesis,
            evidence=evidence,
            signal=K8S_SIGNAL_PAYMENT_API,
        )

        # Find the restart_deployment action
        candidates = plan.get("candidates", [])
        restart_action = None
        for candidate in candidates:
            if candidate["action"] == "restart_deployment":
                restart_action = candidate
                break

        assert restart_action is not None, "restart_deployment action not found"
        assert restart_action["params"]["deployment"] == "payment-api"
        assert restart_action["params"]["namespace"] == "payment-api"

    def test_billing_worker_crashloop_produces_billing_worker_target(self, sqlite_sessions, store):
        """New behavior: billing-worker signal produces billing-worker targets,
        not payment-api."""
        deps, _ = make_deps(
            sqlite_sessions,
            FakeLLM(),
            store.load("crashloop"),
            store,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL_BILLING_WORKER)
        evidence = _evidence("ev_crashloop_1")
        hypothesis = Hypothesis(
            claim="secret missing", evidence_refs=["ev_crashloop_1"], confidence=0.9
        )
        plan = _run_plan(
            deps,
            sqlite_sessions,
            incident_id,
            hypothesis=hypothesis,
            evidence=evidence,
            signal=K8S_SIGNAL_BILLING_WORKER,
        )

        # Find the restart_deployment action
        candidates = plan.get("candidates", [])
        restart_action = None
        for candidate in candidates:
            if candidate["action"] == "restart_deployment":
                restart_action = candidate
                break

        assert restart_action is not None, "restart_deployment action not found"
        assert restart_action["params"]["deployment"] == "billing-worker"
        assert restart_action["params"]["namespace"] == "billing-worker"


class TestManifestPaths:
    """Test that manifest-path actions are only proposed for payment-api."""

    def test_payment_api_crashloop_includes_secret_manifest(self, sqlite_sessions, store):
        """payment-api should propose reapply_manifest with secret path."""
        deps, _ = make_deps(
            sqlite_sessions,
            FakeLLM(),
            store.load("crashloop"),
            store,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL_PAYMENT_API)
        evidence = _evidence("ev_crashloop_1")
        hypothesis = Hypothesis(
            claim="secret missing", evidence_refs=["ev_crashloop_1"], confidence=0.9
        )
        plan = _run_plan(
            deps,
            sqlite_sessions,
            incident_id,
            hypothesis=hypothesis,
            evidence=evidence,
            signal=K8S_SIGNAL_PAYMENT_API,
        )

        # Check for reapply_manifest action with secret path
        candidates = plan.get("candidates", [])
        has_secret_manifest = any(
            c["action"] == "reapply_manifest"
            and c["params"].get("path") == "sandbox/payment-api/k8s/03-secret.yaml"
            for c in candidates
        )
        assert has_secret_manifest, "payment-api should include secret manifest action"

    def test_billing_worker_crashloop_excludes_manifest_paths(self, sqlite_sessions, store):
        """billing-worker should NOT propose payment-api manifest paths."""
        deps, _ = make_deps(
            sqlite_sessions,
            FakeLLM(),
            store.load("crashloop"),
            store,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL_BILLING_WORKER)
        evidence = _evidence("ev_crashloop_1")
        hypothesis = Hypothesis(
            claim="secret missing", evidence_refs=["ev_crashloop_1"], confidence=0.9
        )
        plan = _run_plan(
            deps,
            sqlite_sessions,
            incident_id,
            hypothesis=hypothesis,
            evidence=evidence,
            signal=K8S_SIGNAL_BILLING_WORKER,
        )

        # Check that no payment-api paths are in candidates
        candidates = plan.get("candidates", [])
        for candidate in candidates:
            if candidate["action"] == "reapply_manifest":
                path = candidate["params"].get("path", "")
                assert "payment-api" not in path, (
                    f"billing-worker should not include payment-api path: {path}"
                )

        # Check that no open_fix_pr action is proposed (it uses payment-api files)
        assert not any(c["action"] == "open_fix_pr" for c in candidates), (
            "billing-worker should not propose open_fix_pr"
        )


def test_a_fix_pr_with_a_missing_file_is_not_proposed():
    """A fix PR never goes out missing part of the fix: if any file's
    content cannot be produced, the whole fix PR is dropped."""
    from agent.rules import _fix_pr

    state = {"incident_id": "INC-1", "service": "payment-api"}
    assert _fix_pr(state, {"path": "a", "content": "x"}, None) is None
    assert _fix_pr(state, {"path": "a", "content": "x"}, lambda s: None) is None
    assert _fix_pr(state, {"path": "a", "content": "x"}) is not None
