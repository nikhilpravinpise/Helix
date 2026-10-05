"""The safety-policy access table as code (subphase 1.8).

`decide(action, params, environment)` is pure - these are the verdict
rows from CONTEXT.md section 5 / docs/architecture/safety-policy.md,
exercised directly. Graph-level enforcement lives in test_execution.py;
the code-level write bounds (kube context, protected branch, write root)
live in tests/integrations/test_writes.py.
"""

import pytest

from agent.policy import NON_PROD_ENVIRONMENTS, PolicyDecision, decide

K8S = {"deployment": "payment-api", "namespace": "payment-api"}
DEPLOYMENT_PATH = "sandbox/payment-api/k8s/04-deployment.yaml"
SECRET_PATH = "sandbox/payment-api/k8s/03-secret.yaml"


class TestNonProdTiers:
    """low|medium write in sandbox/staging -> ALLOW_WITH_CHECKS; medium
    additionally requires the declared expected_effect to verify against."""

    @pytest.mark.parametrize("env", sorted(NON_PROD_ENVIRONMENTS))
    def test_low_tier_allowed_with_checks(self, env):
        verdict = decide("restart_deployment", dict(K8S), env)
        assert verdict.decision == PolicyDecision.ALLOW_WITH_CHECKS
        assert verdict.risk_tier == "low"
        assert verdict.verification_required is False

    @pytest.mark.parametrize("env", sorted(NON_PROD_ENVIRONMENTS))
    def test_medium_tier_allowed_with_checks_and_verification(self, env):
        # scale to 3 is medium-tier; the spec's expected_effect must be
        # defined so 1.9 has something to verify against.
        verdict = decide("scale_deployment", {**K8S, "replicas": 3}, env)
        assert verdict.decision == PolicyDecision.ALLOW_WITH_CHECKS
        assert verdict.risk_tier == "medium"
        assert verdict.verification_required is True

    def test_medium_manifest_apply_requires_verification(self):
        verdict = decide("reapply_manifest", {"path": DEPLOYMENT_PATH}, "sandbox")
        assert verdict.decision == PolicyDecision.ALLOW_WITH_CHECKS
        assert verdict.verification_required is True

    def test_rerun_pipeline_is_low(self):
        verdict = decide("rerun_pipeline", {"job": "payment-api"}, "staging")
        assert verdict.decision == PolicyDecision.ALLOW_WITH_CHECKS
        assert verdict.risk_tier == "low"


class TestApprovalGate:
    """Production writes and high-tier actions -> REQUIRE_APPROVAL."""

    @pytest.mark.parametrize(
        "action,params",
        [
            ("restart_deployment", K8S),  # low tier - still gated in prod
            ("rerun_pipeline", {"job": "payment-api"}),
            ("reapply_manifest", {"path": DEPLOYMENT_PATH}),
        ],
    )
    def test_any_write_in_production_requires_approval(self, action, params):
        verdict = decide(action, dict(params), "production")
        assert verdict.decision == PolicyDecision.REQUIRE_APPROVAL
        assert "production" in verdict.reason

    def test_secret_manifest_requires_approval_even_in_sandbox(self):
        verdict = decide("reapply_manifest", {"path": SECRET_PATH}, "sandbox")
        assert verdict.decision == PolicyDecision.REQUIRE_APPROVAL
        assert verdict.risk_tier == "high"

    def test_secret_file_in_fix_pr_requires_approval(self):
        params = {
            "branch": "helix/INC-deadbeef",
            "files": [{"path": SECRET_PATH, "content": "x: y"}],
            "title": "fix",
        }
        verdict = decide("open_fix_pr", params, "sandbox")
        assert verdict.decision == PolicyDecision.REQUIRE_APPROVAL
        assert verdict.risk_tier == "high"

    def test_open_fix_pr_normal_files_are_medium(self):
        params = {
            "branch": "helix/INC-deadbeef",
            "files": [{"path": "sandbox/payment-api/app/main.py", "content": "x"}],
            "title": "fix",
        }
        verdict = decide("open_fix_pr", params, "staging")
        assert verdict.decision == PolicyDecision.ALLOW_WITH_CHECKS
        assert verdict.verification_required is True

    def test_unknown_environment_fails_safe_to_approval(self):
        # An environment outside the known non-prod set is never allowed
        # to write unchecked.
        verdict = decide("restart_deployment", dict(K8S), "qa-mystery")
        assert verdict.decision == PolicyDecision.REQUIRE_APPROVAL


class TestDeny:
    """Out-of-catalogue / unvalidatable -> DENY, before any executor."""

    def test_unknown_action_denied(self):
        verdict = decide("delete_everything", {}, "sandbox")
        assert verdict.decision == PolicyDecision.DENY
        assert "not in the action catalogue" in verdict.reason

    def test_invalid_params_denied(self):
        verdict = decide("scale_deployment", {**K8S, "replicas": 0}, "sandbox")
        assert verdict.decision == PolicyDecision.DENY
        assert "params failed validation" in verdict.reason

    def test_protected_branch_denied(self):
        params = {
            "branch": "main",
            "files": [{"path": "sandbox/payment-api/app/x.py", "content": "x"}],
            "title": "sneaky",
        }
        verdict = decide("open_fix_pr", params, "sandbox")
        assert verdict.decision == PolicyDecision.DENY

    def test_path_outside_write_root_denied(self):
        params = {
            "branch": "helix/INC-deadbeef",
            "files": [{"path": "README.md", "content": "x"}],
            "title": "fix",
        }
        verdict = decide("open_fix_pr", params, "sandbox")
        assert verdict.decision == PolicyDecision.DENY

    def test_manifest_path_outside_allowlist_denied(self):
        verdict = decide("reapply_manifest", {"path": "etc/passwd.yaml"}, "sandbox")
        assert verdict.decision == PolicyDecision.DENY


class TestEvidenceCoherence:
    """The 1.5 ablation's open defect, fixed: an action whose executor
    mutates a running system (kubernetes, jenkins) proposed on evidence
    that only ever touched source control escalates to REQUIRE_APPROVAL
    - the incoherence a human must sign off, rather than a silent allow
    that rolled back a deployment over a rejected PR in the wild.

    The domain is the canonical event TYPE, not the vendor source: a
    `build.failed` arriving through GitHub's check-run webhook is CI
    evidence, while a `pr.rejected` from anywhere is only a code
    signal."""

    CODE_ONLY = frozenset({"pr.rejected"})
    RUNTIME = frozenset({"build.failed"})
    MIXED = frozenset({"pr.rejected", "deployment.failed"})

    def test_k8s_write_on_code_only_evidence_requires_approval(self):
        verdict = decide("rollback_deployment", dict(K8S), "sandbox", evidence_types=self.CODE_ONLY)
        assert verdict.decision == PolicyDecision.REQUIRE_APPROVAL
        assert "runtime" in verdict.reason

    def test_jenkins_write_on_code_only_evidence_requires_approval(self):
        verdict = decide(
            "rerun_pipeline", {"job": "payment-api"}, "sandbox", evidence_types=self.CODE_ONLY
        )
        assert verdict.decision == PolicyDecision.REQUIRE_APPROVAL

    def test_k8s_write_on_cluster_evidence_proceeds_normally(self):
        verdict = decide(
            "rollback_deployment", dict(K8S), "sandbox", evidence_types=frozenset({"pod.failed"})
        )
        assert verdict.decision == PolicyDecision.ALLOW_WITH_CHECKS

    def test_k8s_write_on_ci_only_evidence_requires_approval(self):
        """Live, 2026-10-03: a failing unit test (build.failed) let arm E
        restart and then roll back the sandbox deployment unapproved - the
        rollback landed on an earlier injected fault and left a pod
        crashlooping. CI evidence speaks to the pipeline, not the cluster.
        This test replaced one that asserted the gap as expected behaviour
        (rollback allowed on {"build.failed"}); the rule changed with
        Vraj's confirmation."""
        verdict = decide("rollback_deployment", dict(K8S), "sandbox", evidence_types=self.RUNTIME)
        assert verdict.decision == PolicyDecision.REQUIRE_APPROVAL
        assert "cluster" in verdict.reason

    def test_jenkins_write_on_cluster_only_evidence_requires_approval(self):
        """The same split, the other way: a crashing pod is not evidence
        that re-running the pipeline will help."""
        verdict = decide(
            "rerun_pipeline",
            {"job": "payment-api"},
            "sandbox",
            evidence_types=frozenset({"pod.failed"}),
        )
        assert verdict.decision == PolicyDecision.REQUIRE_APPROVAL

    def test_k8s_write_on_mixed_evidence_proceeds_normally(self):
        verdict = decide("rollback_deployment", dict(K8S), "sandbox", evidence_types=self.MIXED)
        assert verdict.decision == PolicyDecision.ALLOW_WITH_CHECKS

    def test_code_fix_on_code_evidence_is_not_gated(self):
        """open_fix_pr (github executor) is coherent for ANY evidence
        domain - a code fix is always a legitimate remediation shape."""
        params = {
            "branch": "helix/INC-deadbeef",
            "files": [{"path": "sandbox/payment-api/app/x.py", "content": "x=1"}],
            "title": "fix",
        }
        verdict = decide("open_fix_pr", params, "sandbox", evidence_types=self.RUNTIME)
        assert verdict.decision != PolicyDecision.REQUIRE_APPROVAL

    def test_check_run_failure_counts_as_runtime_evidence(self):
        """The check-run case that motivated event-type domains: a
        GitHub-sourced build.failed is CI evidence - rerun_pipeline must
        not be gated just because the webhook vendor was github."""
        verdict = decide(
            "rerun_pipeline", {"job": "payment-api"}, "sandbox", evidence_types=self.RUNTIME
        )
        assert verdict.decision == PolicyDecision.ALLOW_WITH_CHECKS

    def test_no_types_supplied_skips_the_check(self):
        """`None` = caller cannot supply evidence kinds (the executor's
        own enforcement re-decision) - the routing check stays where the
        evidence lives, in the coordinator."""
        verdict = decide("rollback_deployment", dict(K8S), "sandbox")
        assert verdict.decision == PolicyDecision.ALLOW_WITH_CHECKS

    def test_coherence_gate_still_gates_in_production(self):
        """Coherence is additional, never weaker: a code-only incident in
        production still requires approval through the environment rule
        as well."""
        verdict = decide(
            "rollback_deployment", dict(K8S), "production", evidence_types=self.CODE_ONLY
        )
        assert verdict.decision == PolicyDecision.REQUIRE_APPROVAL


class TestFingerprintStability:
    """The verdict carries the same fingerprint identity approvals rows
    and exclusions key on - a granted approval can only bless the exact
    (action, params) it was recorded for."""

    def test_fingerprint_matches_candidate_identity(self):
        from agent.validation import candidate_fingerprint

        verdict = decide("scale_deployment", {**K8S, "replicas": 2}, "sandbox")
        assert verdict.fingerprint == candidate_fingerprint(
            "scale_deployment",
            {"namespace": "payment-api", "deployment": "payment-api", "replicas": 2},
        )

    def test_different_params_different_fingerprint(self):
        a = decide("scale_deployment", {**K8S, "replicas": 2}, "sandbox")
        b = decide("scale_deployment", {**K8S, "replicas": 3}, "sandbox")
        assert a.fingerprint != b.fingerprint


def test_every_scenario_optimal_action_is_coherent_with_its_evidence():
    """The split must not gate any remediation the catalogue says is right:
    each scenario's optimal action, judged against the canonical type its
    own signal or events classify to, has evidence from its executor's
    domain (or needs none - open_fix_pr is coherent with anything)."""
    from agent.actions.catalogue import get_action
    from agent.policy import EXECUTOR_EVIDENCE
    from benchmark import catalog
    from events.classify import classify

    for name, scenario in catalog.load_scenarios().items():
        if "signal" in scenario:
            signal = scenario["signal"]
            evidence = {classify(signal["source"], signal.get("signature") or {}).value}
        else:
            evidence = {e["type"] for e in scenario.get("events", [])}
        optimal = scenario.get("optimal_action")
        spec = get_action(optimal) if optimal else None
        if spec is None or spec.executor not in EXECUTOR_EVIDENCE:
            continue
        assert evidence & EXECUTOR_EVIDENCE[spec.executor], (
            f"{name}: optimal {optimal} ({spec.executor}) has no evidence from its "
            f"domain in {sorted(evidence)}"
        )


class TestSecretPathSpellings:
    """Audit 2026-10-05: the Secret manifest's fix PR needs approval, but
    the same file spelled `sandbox/payment-api/./k8s/03-secret.yaml` or
    with `//` was rated medium and allowed unapproved - the path was only
    stripped at the front and then compared as an exact string."""

    @pytest.mark.parametrize(
        "path",
        [
            "sandbox/payment-api/./k8s/03-secret.yaml",
            "sandbox/payment-api//k8s/03-secret.yaml",
            "./sandbox/payment-api/k8s/./03-secret.yaml",
        ],
    )
    def test_every_spelling_of_the_secret_needs_approval(self, path):
        verdict = decide(
            "open_fix_pr",
            {"branch": "helix/INC-1", "files": [{"path": path, "content": "x"}], "title": "t"},
            "sandbox",
        )
        assert verdict.decision == PolicyDecision.REQUIRE_APPROVAL
        assert verdict.params["files"][0]["path"] == "sandbox/payment-api/k8s/03-secret.yaml"
