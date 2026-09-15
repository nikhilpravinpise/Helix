"""Kubernetes action family executor (subphase 1.8): restart_deployment,
rollback_deployment, scale_deployment, reapply_manifest.

Each call passes through `enforce_execution_gate` first - the family
executor never trusts that routing already checked the gate.
`reapply_manifest` resolves the catalogue-allowlisted repo-relative path
against the repo root and applies the committed file.
"""

from pydantic import BaseModel

from agent.actions.catalogue import ActionSpec
from agent.execution import (
    ExecutionContext,
    ExecutionOutcome,
    ExecutionRefusedError,
    enforce_execution_gate,
)


def execute(ctx: ExecutionContext, spec: ActionSpec, params: BaseModel) -> ExecutionOutcome:
    """Dispatch the kubernetes action through the write capability after
    the executor-side gate check. The transport's result payload becomes
    the executions row's `output`."""
    enforce_execution_gate(ctx, spec, params)
    if spec.name == "restart_deployment":
        output = ctx.kubernetes.rollout_restart(params.deployment, params.namespace)
    elif spec.name == "rollback_deployment":
        output = ctx.kubernetes.rollout_undo(params.deployment, params.namespace)
    elif spec.name == "scale_deployment":
        output = ctx.kubernetes.scale(params.deployment, params.namespace, params.replicas)
    elif spec.name == "reapply_manifest":
        manifest = ctx.repo_root / params.path
        output = ctx.kubernetes.apply_manifest(str(manifest))
    else:
        raise ExecutionRefusedError(
            f"{spec.name}: no kubernetes write op for this catalogue action"
        )
    return ExecutionOutcome(detail=f"{spec.name} applied via kubernetes write", output=output)
