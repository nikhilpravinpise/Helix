"""Execute graph node (subphase 1.8): run the selected, policy-cleared
action through its family executor and persist the audit row.

The node only ever runs while the incident status is `executing` - the
coordinator took the `validating -> executing` (or `awaiting_approval ->
executing`) edge first, so the transition rows always bracket the
mutation. Inside the node the executor-side gate (`enforce_execution_gate`)
re-decides the policy verdict and re-queries the approvals table, so the
write cannot proceed on routing's word alone.

Every attempt - success, transport error, or refusal - lands in the
`executions` table with the policy decision, approval ref, approver,
timestamps, origin (live|fixture) and the transport's output payload
(for fixture fakes: the recorded intended write the benchmark scores).
The node never raises on transport failure: the row says `error` and the
coordinator takes `executing -> failed`; refusals say `refused` and take
the same edge. Nothing reaches the transport when the gate refuses.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from agent.actions.catalogue import ActionSpec, get_action
from agent.agents.validation import REPO_ROOT
from agent.execution import (
    ExecutionContext,
    ExecutionRefusedError,
    enforce_execution_gate,
    execution_idempotency_key,
    executor_for,
    persist_execution,
)
from agent.policy import PolicyDecision, decide
from agent.state import WorkflowState
from helix.observability import trace_workflow_step
from integrations.writes import WriteError, WriteRefusedError
from state import models


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Attempt:
    """One execution attempt's outcome, ready to persist."""

    result: models.ExecutionResult
    detail: str
    output: dict[str, Any] | None
    approval: models.Approval | None


def _run_attempt(ctx: ExecutionContext, spec: ActionSpec, params: BaseModel) -> _Attempt:
    """Gate + dispatch. Refusals and transport errors become result
    values, never exceptions - the audit row must exist either way."""
    try:
        _verdict, approval = enforce_execution_gate(ctx, spec, params)
        outcome = executor_for(spec)(ctx, spec, params)
        return _Attempt(models.ExecutionResult.SUCCESS, outcome.detail, outcome.output, approval)
    except (ExecutionRefusedError, WriteRefusedError) as exc:
        return _Attempt(models.ExecutionResult.REFUSED, str(exc), None, None)
    except WriteError as exc:
        return _Attempt(models.ExecutionResult.ERROR, str(exc), None, None)
    except Exception as exc:
        return _Attempt(models.ExecutionResult.ERROR, f"{type(exc).__name__}: {exc}", None, None)


def execution_node(deps):
    """The `execute` node: policy re-check + transport dispatch + audit
    row. Returns state updates (`execution`, `policy`); status transitions
    stay with the coordinator."""

    def _execute(state: WorkflowState) -> dict[str, Any]:
        with trace_workflow_step("execute"):
            selected = state.get("selected_action") or {}
            action = str(selected.get("action") or "")
            raw_params = dict(selected.get("params") or {})
            environment = state.get("environment") or ""
            started = _now()

            spec = get_action(action) if action else None
            params = None
            if spec is not None:
                try:
                    params = spec.params_model.model_validate(raw_params)
                except ValidationError:
                    params = None
            normalized = params.model_dump(mode="json") if params is not None else raw_params
            verdict = decide(action or "(none)", normalized, environment)
            key = execution_idempotency_key(
                state["incident_pk"], verdict.fingerprint, state.get("attempts", 0)
            )

            with deps.session_scope() as session:
                existing = session.scalar(
                    select(models.Execution).where(models.Execution.idempotency_key == key)
                )
                if existing is not None:
                    # Idempotent replay (1.10): this exact write was
                    # already attempted - a crashed run re-entering at
                    # `executing` replays the recorded row's outcome
                    # instead of re-firing the transport. The unique
                    # index is the backstop; this lookup is what makes
                    # the replay return the SAME result.
                    return {
                        "policy": verdict.to_state(),
                        "execution": {
                            "execution_id": existing.id,
                            "action": existing.action,
                            "params": existing.params,
                            "fingerprint": existing.fingerprint,
                            "result": existing.result.value,
                            "detail": f"{existing.detail} (idempotent replay)",
                            "policy_decision": existing.policy_decision,
                            "approval_id": existing.approval_id,
                            "decided_by": existing.decided_by,
                        },
                    }
                if spec is None or params is None or verdict.decision == PolicyDecision.DENY:
                    # Refused before any transport call: unknown action,
                    # unvalidatable params, or a policy DENY. Audited the
                    # same as a real attempt.
                    attempt = _Attempt(models.ExecutionResult.REFUSED, verdict.reason, None, None)
                else:
                    ctx = ExecutionContext(
                        session=session,
                        incident_pk=state["incident_pk"],
                        incident_id=state["incident_id"],
                        environment=environment,
                        kubernetes=deps.kubernetes_write,
                        github=deps.github_write,
                        jenkins=deps.jenkins_write,
                        repo_root=REPO_ROOT,
                        origin=deps.write_origin,
                        kube_evidence=deps.kubernetes,
                    )
                    attempt = _run_attempt(ctx, spec, params)
                execution_id = persist_execution(
                    session,
                    incident_pk=state["incident_pk"],
                    attempt=state.get("attempts", 0),
                    verdict=verdict,
                    approval=attempt.approval,
                    result=attempt.result,
                    detail=attempt.detail,
                    output=attempt.output,
                    origin=deps.write_origin,
                    started_at=started,
                    finished_at=_now(),
                )

            approval = attempt.approval
            return {
                "policy": verdict.to_state(),
                "execution": {
                    "execution_id": execution_id,
                    "action": verdict.action,
                    "params": verdict.params,
                    "fingerprint": verdict.fingerprint,
                    "result": attempt.result.value,
                    "detail": attempt.detail,
                    "policy_decision": verdict.decision.value,
                    "approval_id": approval.id if approval else None,
                    "decided_by": approval.decided_by if approval else None,
                },
            }

    return _execute
