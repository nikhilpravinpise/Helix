"""`kubernetes_target` validator (subphase 1.7): restart/rollback/scale.

Two read-side checks, per the action spec's preconditions:

1. The target Deployment exists in the namespace - via the kubernetes
   evidence capability (a 404 is a definitive "the action has no target",
   which is FAIL; a check that could not run is NOT_VALIDATABLE).
2. A `can_i` authorization precheck for the verb the executor will need:
   rollout restart and rollout undo PATCH the deployment; scale UPDATEs
   its scale subresource. A denied answer is FAIL - the action would be
   refused at execution.

Param sanity (replicas >= 1, non-empty names) is already enforced at
expression by the params models and is not re-checked here.
"""

from pydantic import BaseModel

from agent.actions.catalogue import ActionSpec
from agent.validation import ValidationContext, ValidationResult
from integrations.evidence import EvidenceError, EvidenceNotFoundError
from state.models import ValidationOutcome

#: The verb + resource each action's executor will need (1.8):
#: rollout restart/undo patch the Deployment; scale updates its
#: scale subresource. `can_i` answers before any of that runs.
_VERB_RESOURCE: dict[str, tuple[str, str]] = {
    "restart_deployment": ("patch", "deployments"),
    "rollback_deployment": ("patch", "deployments"),
    "scale_deployment": ("update", "deployments/scale"),
}


def validate_kubernetes_target(
    spec: ActionSpec, params: BaseModel, ctx: ValidationContext
) -> ValidationResult:
    deployment = params.deployment
    namespace = params.namespace
    try:
        ctx.kubernetes.get_deployment_status(namespace, deployment)
    except EvidenceNotFoundError:
        return ValidationResult(
            outcome=ValidationOutcome.FAIL,
            reason=f"deployment {deployment} does not exist in namespace {namespace}",
        )
    except EvidenceError as exc:
        return ValidationResult(
            outcome=ValidationOutcome.NOT_VALIDATABLE,
            reason=f"could not verify deployment existence: {exc}",
        )

    verb, resource = _VERB_RESOURCE[spec.name]
    try:
        check = ctx.prechecks.can_i(verb, resource, namespace, group="apps")
    except EvidenceError as exc:
        return ValidationResult(
            outcome=ValidationOutcome.NOT_VALIDATABLE,
            reason=f"could not run authorization check: {exc}",
        )
    if not check.ok:
        return ValidationResult(
            outcome=ValidationOutcome.FAIL,
            reason=(f"not authorized to {verb} {resource} in {namespace}: {check.detail}"),
            details={"can_i": check.checks},
        )
    return ValidationResult(
        outcome=ValidationOutcome.PASS,
        reason=f"deployment {deployment} exists in {namespace}; {verb} {resource} permitted",
        details={"can_i": check.checks},
    )
