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


def decide(action: str, params: dict[str, Any] | None, environment: str) -> PolicyVerdict:
    """Map (action, params, environment) onto the access-table verdict.

    Pure: catalogue lookup + params re-validation + the tier/environment
    rules. Callers: the coordinator (routing - which edge out of
    `validating`) and the executor's own gate (enforcement - refuses DENY
    outright and REQUIRE_APPROVAL without a granted approvals row).
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
