"""Compensation node (subphase 1.10): undo a compensable action whose
verification failed, BEFORE the coordinator decides the next cycle.

The catalogue's `compensating_action` metadata names the undo:

- `scale_deployment` -> `scale_deployment` back to the replica count the
  executor captured in `executions.output["previous_replicas"]` at write
  time (agent/execution/kubernetes.py). A row without the count cannot
  derive its undo - recorded as refused and escalated, never guessed.
- `rollback_deployment` -> `rollback_deployment` again (roll forward to
  the pre-rollback revision).
- `reapply_manifest` -> `reapply_manifest` of the same committed manifest
  (re-assert the allowlisted desired state).
- `open_fix_pr` -> `close_pr`, an executor-side op the planner can never
  propose (it is not a catalogue action): the incident branch stays as
  the audit trail but the PR is withdrawn.

Dispatch reuses the executor layer: catalogue compensations pass through
`enforce_execution_gate` exactly like remediations (a compensating write
is still a write); executor-side ops run under the failed action's own
authorization record. Every attempt lands in `executions` with
`compensates_id` pointing at the verification-failed row - that link is
what pays the `needs_compensation` debt (only a `success` pays it) and
what provenance renders as the undo.

Failure semantics: a compensation that errors, is refused, or cannot be
derived is recorded and the coordinator escalates - a compensable
failure Helix cannot undo is exactly the human-required case, and
retrying the undo inside this node would be an unbounded inner loop.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import select

from agent.actions.catalogue import ActionSpec, get_action
from agent.agents.execution import _run_attempt
from agent.agents.validation import REPO_ROOT
from agent.execution import (
    ExecutionContext,
    execution_idempotency_key,
    persist_execution,
)
from agent.policy import PolicyDecision, PolicyVerdict, decide
from agent.state import WorkflowState
from agent.validation import candidate_fingerprint
from helix.observability import trace_workflow_step
from integrations.writes import WriteError, WriteRefusedError
from state import models

if TYPE_CHECKING:
    from agent.graph import GraphDeps


def _now() -> datetime:
    return datetime.now(UTC)


def _paid_compensations(session, incident_pk: int):
    """Scalar select of execution ids whose compensation SUCCEEDED - only
    a `success` pays the debt; an errored/refused attempt leaves the
    change un-undone and the debt outstanding."""
    return (
        select(models.Execution.compensates_id)
        .where(models.Execution.incident_id == incident_pk)
        .where(models.Execution.compensates_id.isnot(None))
        .where(models.Execution.result == models.ExecutionResult.SUCCESS)
    )


def outstanding_compensation(session, incident_pk: int) -> bool:
    """Whether any failed verification's recorded debt is still unpaid:
    a `needs_compensation` verifications row whose execution has no
    successful compensating executions row. This is the shared definition
    intake (graph.py), the verify node and this node all agree on."""
    return _unpaid_debt(session, incident_pk) is not None


def _unpaid_debt(session, incident_pk: int) -> models.Verification | None:
    """The newest unpaid compensation debt, if any."""
    paid = _paid_compensations(session, incident_pk)
    return session.scalar(
        select(models.Verification)
        .where(models.Verification.incident_id == incident_pk)
        .where(models.Verification.needs_compensation.is_(True))
        .where(
            models.Verification.execution_id.is_(None)
            | models.Verification.execution_id.not_in(paid)
        )
        .order_by(models.Verification.id.desc())
        .limit(1)
    )


def _derive_compensation(
    spec: ActionSpec, failed: models.Execution
) -> tuple[str | None, dict[str, Any], str | None]:
    """(compensating action, params, underivable-reason).

    The catalogue names the ACTION; the params come from the failed
    execution's recorded params plus, where the undo needs state the
    action replaced, what the executor captured in `output` at write
    time. An underivable undo is a refusal, not a guess."""
    comp = spec.compensating_action
    params = dict(failed.params or {})
    if not comp:
        return None, params, f"{spec.name} declares no compensating action"
    if spec.name == "scale_deployment":
        previous = (failed.output or {}).get("previous_replicas")
        if previous is None:
            return (
                comp,
                params,
                "scale_deployment recorded no previous_replicas; "
                "the scale-back target cannot be derived",
            )
        params["replicas"] = int(previous)
    elif comp == "close_pr":
        params = {
            "branch": params.get("branch"),
            "pr_number": (failed.output or {}).get("pr_number"),
        }
        if not params["branch"]:
            return (
                comp,
                params,
                "open_fix_pr recorded no branch; close_pr has nothing to target",
            )
    return comp, params, None


#: Executor-side compensation ops the planner can never propose (they are
#: not catalogue actions): `close_pr` withdraws the open_fix_pr PR.
_COMP_OPS: frozenset[str] = frozenset({"close_pr"})


@dataclass(frozen=True)
class _OpAttempt:
    """The outcome of an executor-side compensation op (non-catalogue)."""

    result: models.ExecutionResult
    detail: str
    output: dict[str, Any] | None


def _op_attempt(ctx: ExecutionContext, comp: str, params: dict[str, Any]) -> _OpAttempt:
    """Dispatch a non-catalogue compensating op. Same result mapping as
    _run_attempt: refusals and transport errors become result values so
    the audit row exists either way."""
    try:
        if comp == "close_pr":
            output = ctx.github.close_pr(branch=params["branch"], pr_number=params.get("pr_number"))
            return _OpAttempt(
                models.ExecutionResult.SUCCESS,
                f"close_pr withdrew the PR on {params['branch']}",
                output,
            )
        return _OpAttempt(
            models.ExecutionResult.REFUSED,
            f"no compensating op registered for {comp!r}",
            None,
        )
    except WriteRefusedError as exc:
        return _OpAttempt(models.ExecutionResult.REFUSED, str(exc), None)
    except WriteError as exc:
        return _OpAttempt(models.ExecutionResult.ERROR, str(exc), None)
    except Exception as exc:
        return _OpAttempt(models.ExecutionResult.ERROR, f"{type(exc).__name__}: {exc}", None)


def _compensation_state(
    result: models.ExecutionResult,
    detail: str,
    *,
    action: str | None,
    params: dict[str, Any] | None,
    execution_id: int | None,
    compensates_id: int | None,
) -> dict[str, Any]:
    """The state["compensation"] summary the coordinator routes on."""
    return {
        "execution_id": execution_id,
        "action": action,
        "params": params,
        "result": result.value,
        "detail": detail,
        "compensates_id": compensates_id,
    }


def compensation_node(deps: "GraphDeps"):
    """The `compensate` graph node: pay the newest unpaid compensation
    debt recorded by a failed verification, then hand back to the
    coordinator. Status transitions stay coordinator-owned."""

    def _compensate(state: WorkflowState) -> dict[str, Any]:
        with trace_workflow_step("compensate"):
            errors: list[str] = list(state.get("errors", []))
            with deps.session_scope() as session:
                debt = _unpaid_debt(session, state["incident_pk"])
                failed = (
                    session.get(models.Execution, debt.execution_id)
                    if debt is not None and debt.execution_id is not None
                    else None
                )
                started = _now()

                if debt is None:
                    # Defensive: the router only sends here on an unpaid
                    # debt; a resume racing a crash could arrive after it
                    # was already paid - nothing to do, let the
                    # coordinator proceed to the bounded replan.
                    compensation = _compensation_state(
                        models.ExecutionResult.SUCCESS,
                        "no outstanding compensation debt",
                        action=None,
                        params=None,
                        execution_id=None,
                        compensates_id=None,
                    )
                elif failed is None:
                    # A debt without its execution row is a corruption
                    # case - no undo is derivable. Escalate via the
                    # non-success result; nothing invented gets recorded.
                    compensation = _compensation_state(
                        models.ExecutionResult.ERROR,
                        "compensation debt has no execution to undo",
                        action=None,
                        params=None,
                        execution_id=None,
                        compensates_id=None,
                    )
                else:
                    compensation = _run_compensation(session, deps, state, failed, started, errors)
                outstanding = outstanding_compensation(session, state["incident_pk"])
            return {
                "compensation": compensation,
                "needs_compensation": outstanding,
                "errors": errors,
            }

    return _compensate


def _run_compensation(
    session, deps: "GraphDeps", state: WorkflowState, failed: models.Execution, started, errors
) -> dict[str, Any]:
    """Derive, dispatch and persist the compensation for `failed`."""
    incident_pk = state["incident_pk"]
    attempt = state.get("attempts", 0)
    environment = state.get("environment") or ""
    spec = get_action(failed.action)
    comp, params, underivable = (
        _derive_compensation(spec, failed)
        if spec is not None
        else (None, {}, f"action {failed.action!r} is not in the catalogue")
    )
    key = execution_idempotency_key(
        incident_pk, candidate_fingerprint(comp or "(none)", params), attempt, failed.id
    )
    # Idempotent replay (1.10): one compensation attempt per failed
    # execution. A crash after the row persisted but before the state
    # update replays the recorded outcome instead of undoing twice or
    # colliding on the unique key. A non-success row replays as its
    # recorded result - the coordinator escalates the same way, so a
    # retried run cannot hang or silently re-fire the undo.
    existing = session.scalar(
        select(models.Execution).where(models.Execution.idempotency_key == key)
    )
    if existing is not None:
        return _compensation_state(
            existing.result,
            f"{existing.detail} (idempotent replay)",
            action=existing.action,
            params=existing.params,
            execution_id=existing.id,
            compensates_id=existing.compensates_id,
        )

    ctx = ExecutionContext(
        session=session,
        incident_pk=incident_pk,
        incident_id=state["incident_id"],
        environment=environment,
        kubernetes=deps.kubernetes_write,
        github=deps.github_write,
        jenkins=deps.jenkins_write,
        repo_root=REPO_ROOT,
        origin=deps.write_origin,
        kube_evidence=deps.kubernetes,
    )
    # The compensation inherits the failed execution's authorization
    # record: it undoes exactly what that approval blessed.
    inherited = session.get(models.Approval, failed.approval_id) if failed.approval_id else None

    comp_spec = get_action(comp) if comp is not None else None
    unknown_comp = comp is not None and comp_spec is None and comp not in _COMP_OPS
    if underivable is not None or unknown_comp:
        # Underivable or unregistered undo: refused before any transport
        # call - a compensation never guesses its params.
        reason = underivable or f"no catalogue action or op named {comp!r}"
        verdict = PolicyVerdict(
            decision=PolicyDecision.DENY,
            action=comp or "(none)",
            risk_tier=None,
            reason=reason,
            verification_required=False,
            params=params,
            fingerprint=candidate_fingerprint(comp or "(none)", params),
        )
        result, detail, output, approval = (
            models.ExecutionResult.REFUSED,
            reason,
            None,
            inherited,
        )
        policy_decision = "compensation"
    elif comp_spec is not None:
        # Catalogue compensating action: the executor-side policy gate
        # applies exactly like a remediation - a compensating write is
        # still a write.
        invalid_reason: str | None = None
        try:
            validated = comp_spec.params_model.model_validate(params)
            normalized = validated.model_dump(mode="json")
        except ValidationError as exc:
            validated, normalized = None, params
            invalid_reason = f"compensating params failed validation: {exc.errors()[0]['msg']}"
        if validated is None:
            verdict = PolicyVerdict(
                decision=PolicyDecision.DENY,
                action=comp_spec.name,
                risk_tier=None,
                reason=invalid_reason or "compensating params failed validation",
                verification_required=False,
                params=normalized,
                fingerprint=candidate_fingerprint(comp_spec.name, normalized),
            )
            result, detail, output, approval = (
                models.ExecutionResult.REFUSED,
                verdict.reason,
                None,
                inherited,
            )
            policy_decision = "compensation"
        else:
            verdict = decide(comp_spec.name, normalized, environment)
            ran = _run_attempt(ctx, comp_spec, validated)
            result, detail, output = ran.result, ran.detail, ran.output
            approval = ran.approval or inherited
            policy_decision = verdict.decision.value
    else:
        # Executor-side op (close_pr): no catalogue spec exists, so no
        # policy verdict ran; the row records "compensation" rather than
        # a fabricated allow.
        verdict = PolicyVerdict(
            decision=PolicyDecision.ALLOW_WITH_CHECKS,
            action=comp,
            risk_tier=None,
            reason="compensation for a previously-authorized execution",
            verification_required=False,
            params=params,
            fingerprint=candidate_fingerprint(comp, params),
        )
        ran = _op_attempt(ctx, comp, params)
        result, detail, output = ran.result, ran.detail, ran.output
        approval = inherited
        policy_decision = "compensation"

    row_id = persist_execution(
        session,
        incident_pk=incident_pk,
        attempt=attempt,
        verdict=verdict,
        approval=approval,
        result=result,
        detail=detail,
        output=output,
        origin=(output or {}).get("origin", deps.write_origin),
        started_at=started,
        finished_at=_now(),
        compensates_id=failed.id,
        policy_decision=policy_decision,
    )
    if result != models.ExecutionResult.SUCCESS:
        errors.append(f"compensate: {comp} failed - {detail}")
    return _compensation_state(
        result,
        detail,
        action=verdict.action,
        params=verdict.params,
        execution_id=row_id,
        compensates_id=failed.id,
    )
