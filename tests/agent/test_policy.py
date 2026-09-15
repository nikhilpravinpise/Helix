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
