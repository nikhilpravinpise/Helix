"""GitHub action family executor (subphase 1.8): open_fix_pr - the
durable-fix path (branch + commit + PR; Helix proposes, humans merge)."""

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
    check. The client itself refuses non-helix/INC-* branches and paths
    outside the write root before any HTTP call."""
    enforce_execution_gate(ctx, spec, params)
    if spec.name != "open_fix_pr":
        raise ExecutionRefusedError(f"{spec.name}: no github write op for this catalogue action")
    output = ctx.github.open_fix_pr(
        branch=params.branch,
        files=[{"path": f.path, "content": f.content} for f in params.files],
        title=params.title,
        body=params.body,
    )
    pr = output.get("pr_url") or output.get("pr_number") or "recorded"
    return ExecutionOutcome(detail=f"open_fix_pr opened: {pr}", output=output)
