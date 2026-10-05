"""Safety policy engine (subphase 1.8): the access table as code.

`decide(action, params, environment)` maps a candidate action plus the
incident's target environment to one of four verdicts, encoding the
CONTEXT.md section 5 access table:

- read -> allow: the evidence layer (1.3) is the read path; writes never
  reach here.
- low | medium write, non-prod -> ALLOW_WITH_CHECKS. Medium additionally
  requires verification to be defined: the action's `expected_effect`
  (what 1.9 checks against) must exist - a medium-risk action with no
  declared effect is escalated to REQUIRE_APPROVAL rather than allowed
  unchecked.
- ANY write where `environment == "production"`, any `high`-tier action
  (secret changes, destructive ops) -> REQUIRE_APPROVAL. Unknown
  environments fail safe into the same gate.
- action not in the catalogue, params failing the spec's params_model ->
  DENY. DENY is also enforced in code at the executor (it refuses before
  any transport call), so a policy bug cannot smuggle a denied action
  through.

Environment comes from the incident record (set at intake), never
inferred: `sandbox` and `staging` are non-prod; `production` - and any
value outside the known non-prod set - forces the approval path.

`docs/architecture/safety-policy.md` carries the rendered table and the
fault -> action -> gate mapping.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from agent.actions.catalogue import RiskTier, get_action
from agent.validation import candidate_fingerprint


class PolicyDecision(StrEnum):
    """The access-table verdicts."""

    ALLOW = "allow"
    ALLOW_WITH_CHECKS = "allow_with_checks"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


#: Incident target environments that count as non-production. `production`
#: and anything unexpected both fail safe into REQUIRE_APPROVAL.
NON_PROD_ENVIRONMENTS: frozenset[str] = frozenset({"sandbox", "staging"})

#: Executors that mutate a running system, and the observed domain each
#: one's writes must be grounded in. `github` is deliberately absent: a fix
#: PR is the legitimate remediation shape for evidence from ANY domain (a
#: code review can fix a k8s manifest).
#:
#: The type, not the vendor source, carries the meaning: a `build.failed`
#: surfaced through GitHub's check-run webhook is still CI evidence, and a
#: `pr.rejected` from anywhere is only a code signal. Cluster and pipeline
#: are separate domains. They were one "runtime" set until a live run on
#: 2026-10-03, where a failing unit test (build.failed) let arm E restart
#: and then roll back the sandbox deployment unapproved - the rollback
#: landed on an earlier injected fault. CI evidence says nothing about the
#: cluster, and a crashing pod says nothing about the pipeline.
EXECUTOR_EVIDENCE: dict[str, frozenset[str]] = {
    "kubernetes": frozenset(
        {
            "deployment.started",
            "deployment.failed",
            "deployment.succeeded",
            "pod.failed",
            "service.unhealthy",
        }
    ),
    "jenkins": frozenset({"build.started", "build.passed", "build.failed"}),
}


@dataclass(frozen=True)
class PolicyVerdict:
    """The outcome of a policy decision: the verdict, the action it was
    computed for, the resolved risk tier (None when the action is not in
    the catalogue), the human-readable reason, whether post-action
    verification is required (medium tier), the normalized params, and
    the candidate fingerprint - the identity approvals/executions rows
    and the exclusion list all key on."""

    decision: PolicyDecision
    action: str
    risk_tier: str | None
    reason: str
    verification_required: bool
    params: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""

    def to_state(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "action": self.action,
            "risk_tier": self.risk_tier,
            "reason": self.reason,
            "verification_required": self.verification_required,
            "fingerprint": self.fingerprint,
        }


def decide(
    action: str,
    params: dict[str, Any] | None,
    environment: str,
    evidence_types: "frozenset[str] | None" = None,
) -> PolicyVerdict:
    """Map (action, params, environment) onto the access-table verdict.

    Pure: catalogue lookup + params re-validation + the tier/environment
    rules. Callers: the coordinator (routing - which edge out of
    `validating`) and the executor's own gate (enforcement - refuses DENY
    outright and REQUIRE_APPROVAL without a granted approvals row).

    `evidence_types` is the set of canonical event types the incident's
    evidence actually classifies as (signals via events.classify, event
    records directly). When given, a runtime-mutating action
    (kubernetes/jenkins executor) proposed on evidence that never
    observed a runtime system - a code-review-only incident, say - is
    escalated to REQUIRE_APPROVAL: a human must deliberately authorize a
    cluster write that no runtime signal justifies, which is the
    coherence check the 1.5 evaluation found missing (arm B rolled back
    a deployment over a rejected PR). `None` means the caller cannot
    supply types - the check is skipped, which the enforcement gate
    relies on: it re-decides a row the coordinator already judged, and
    the coherence verdict was part of that first decision.
    """
    raw_params = dict(params or {})
    spec = get_action(action)
    if spec is None:
        return PolicyVerdict(
            decision=PolicyDecision.DENY,
            action=action or "(none)",
            risk_tier=None,
            reason=f"action {action!r} is not in the action catalogue",
            verification_required=False,
            params=raw_params,
            fingerprint=candidate_fingerprint(action or "(none)", raw_params),
        )
    try:
        validated = spec.params_model.model_validate(raw_params)
    except ValidationError as exc:
        return PolicyVerdict(
            decision=PolicyDecision.DENY,
            action=spec.name,
            risk_tier=None,
            reason=f"params failed validation: {exc.errors()[0]['msg']}",
            verification_required=False,
            params=raw_params,
            fingerprint=candidate_fingerprint(spec.name, raw_params),
        )

    normalized = validated.model_dump(mode="json")
    fingerprint = candidate_fingerprint(spec.name, normalized)
    tier = spec.risk_tier(validated)

    required = EXECUTOR_EVIDENCE.get(spec.executor)
    if evidence_types is not None and required is not None and not (evidence_types & required):
        # Evidence coherence (the 1.5 ablation's defect, narrowed per
        # executor): a write to the cluster or the pipeline needs evidence
        # from that same domain. Escalate to the human gate rather than
        # deny outright - the action may still be right, but only a person
        # may say so.
        kinds = ",".join(sorted(evidence_types)) or "none"
        domain = "cluster" if spec.executor == "kubernetes" else "pipeline"
        return PolicyVerdict(
            decision=PolicyDecision.REQUIRE_APPROVAL,
            action=spec.name,
            risk_tier=tier.value,
            reason=(
                f"{spec.executor} write proposed on evidence of only [{kinds}]: "
                f"a runtime-mutating {domain} action needs {domain} evidence "
                "or a human's explicit approval"
            ),
            verification_required=True,
            params=normalized,
            fingerprint=fingerprint,
        )

    namespace = normalized.get("namespace")
    if spec.executor == "kubernetes" and namespace is not None:
        from agent.ops.registry import load_registry

        if namespace not in load_registry().namespaces():
            # Deployment and namespace are the planner's free strings: a
            # restart of kube-system/coredns on pod.failed evidence was
            # allowed with no human. Helix acts alone only where a
            # registered service runs.
            return PolicyVerdict(
                decision=PolicyDecision.REQUIRE_APPROVAL,
                action=spec.name,
                risk_tier=tier.value,
                reason=(
                    f"namespace {namespace!r} belongs to no registered service: "
                    "a write there needs a human's approval"
                ),
                verification_required=True,
                params=normalized,
                fingerprint=fingerprint,
            )

    if environment not in NON_PROD_ENVIRONMENTS:
        label = "production" if environment == "production" else f"unknown ({environment!r})"
        return PolicyVerdict(
            decision=PolicyDecision.REQUIRE_APPROVAL,
            action=spec.name,
            risk_tier=tier.value,
            reason=f"target environment is {label}: any write requires approval",
            verification_required=True,
            params=normalized,
            fingerprint=fingerprint,
        )
    if tier == RiskTier.HIGH:
        return PolicyVerdict(
            decision=PolicyDecision.REQUIRE_APPROVAL,
            action=spec.name,
            risk_tier=tier.value,
            reason="high-risk action (secret change or destructive op) requires approval",
            verification_required=True,
            params=normalized,
            fingerprint=fingerprint,
        )
    if tier == RiskTier.MEDIUM:
        if not spec.expected_effect.strip():
            # A medium-risk action with no declared expected effect cannot
            # be verified after the fact - escalate to the human gate
            # rather than letting it run unchecked.
            return PolicyVerdict(
                decision=PolicyDecision.REQUIRE_APPROVAL,
                action=spec.name,
                risk_tier=tier.value,
                reason="medium-risk action has no expected_effect to verify against",
                verification_required=True,
                params=normalized,
                fingerprint=fingerprint,
            )
        return PolicyVerdict(
            decision=PolicyDecision.ALLOW_WITH_CHECKS,
            action=spec.name,
            risk_tier=tier.value,
            reason="medium-risk write in non-production; verification required",
            verification_required=True,
            params=normalized,
            fingerprint=fingerprint,
        )
    return PolicyVerdict(
        decision=PolicyDecision.ALLOW_WITH_CHECKS,
        action=spec.name,
        risk_tier=tier.value,
        reason="low-risk write in non-production",
        verification_required=False,
        params=normalized,
        fingerprint=fingerprint,
    )
