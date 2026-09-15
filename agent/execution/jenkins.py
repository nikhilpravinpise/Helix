"""Jenkins action family executor (subphase 1.8): rerun_pipeline -
trigger a build of the failed job."""

from pydantic import BaseModel

from agent.actions.catalogue import ActionSpec
from agent.execution import (
    ExecutionContext,
    ExecutionOutcome,
    ExecutionRefusedError,
    enforce_execution_gate,
)


def execute(ctx: ExecutionContext, spec: ActionSpec, params: BaseModel) -> ExecutionOutcome:
    """Dispatch through the write capability after the executor-side gate
    check."""
    enforce_execution_gate(ctx, spec, params)
    if spec.name != "rerun_pipeline":
        raise ExecutionRefusedError(f"{spec.name}: no jenkins write op for this catalogue action")
    output = ctx.jenkins.trigger_build(params.job)
    return ExecutionOutcome(
        detail=f"rerun_pipeline triggered build for job {params.job}", output=output
    )
