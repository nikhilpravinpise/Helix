"""Executors: one per action family, keyed by the catalogue `executor`
key (subphase 1.8).

Two layers enforce the safety policy, deliberately:

1. The COORDINATOR routes on `agent.policy.decide()` - which edge out of
   `validating` (executing vs awaiting_approval vs replan) - and writes
   the approvals row the gate parks on.
2. `enforce_execution_gate()` runs INSIDE the executor, immediately
   before any transport call: it re-decides the policy verdict itself and
   queries the approvals table for a granted row matching the exact
   (action, params) fingerprint. A REQUIRE_APPROVAL action without that
   row - or any DENY - is refused no matter what routing said. A bug in
   the coordinator cannot smuggle a gated write through.

Executors are plain functions `(ExecutionContext, ActionSpec, params) ->
ExecutionOutcome`; they only see the write-capability Protocols and plain
dicts, never vendor SDK types (C8).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.actions.catalogue import ActionSpec
from agent.policy import PolicyDecision, PolicyVerdict, decide
from integrations.capabilities import KubernetesEvidence
from integrations.writes import GitHubWrite, JenkinsWrite, KubernetesWrite
from state import models
from state.models import execution_idempotency_key as execution_idempotency_key


class ExecutionRefusedError(RuntimeError):
    """The executor's own gate refused the action: policy DENY, or
    REQUIRE_APPROVAL without a granted approvals row. Raised before any
    transport call - a refusal is not a failure, nothing executed."""


@dataclass(frozen=True)
class ExecutionOutcome:
    """What a successful executor call reports back. `output` is the
    transport's result payload (for fakes: the recorded intended write);
    it lands verbatim in `executions.output`."""

    detail: str
    output: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionContext:
    """Everything an executor needs, injected by the execute node: the
    open DB session (for the approval-row lookup), the incident identity,
    the write capabilities, and the repo root manifest paths resolve
    against."""

    session: Session
    incident_pk: int
    incident_id: str
    environment: str
    kubernetes: KubernetesWrite
    github: GitHubWrite
    jenkins: JenkinsWrite
    repo_root: Path
    origin: str  # "live" | "fixture"
    # 1.10: the READ-side kubernetes evidence capability, used by
    # executors that must observe pre-write state for compensation (e.g.
    # scale_deployment records the replica count it is replacing so a
    # scale-back knows where to land). Optional so executor-level tests
    # that never scale need not supply it.
    kube_evidence: KubernetesEvidence | None = None


def _granted_approval(
    session: Session, incident_pk: int, fingerprint: str
) -> models.Approval | None:
    """The newest APPROVED approvals row for this exact (action, params)
    fingerprint on this incident, or None."""
    return session.scalar(
        select(models.Approval)
        .where(models.Approval.incident_id == incident_pk)
        .where(models.Approval.fingerprint == fingerprint)
        .where(models.Approval.status == models.ApprovalStatus.APPROVED)
        .order_by(models.Approval.id.desc())
        .limit(1)
    )


def latest_selected_candidate_id(session: Session, incident_pk: int) -> int | None:
    """The newest selected candidates row id - the candidate link the
    audit rows (executions, approvals) carry."""
    return session.scalar(
        select(models.Candidate.id)
        .where(models.Candidate.incident_id == incident_pk)
        .where(models.Candidate.selected.is_(True))
        .order_by(models.Candidate.id.desc())
        .limit(1)
    )


def persist_execution(
    session: Session,
    *,
    incident_pk: int,
    attempt: int,
    verdict: PolicyVerdict,
    approval: models.Approval | None,
    result: models.ExecutionResult,
    detail: str,
    output: dict[str, Any] | None,
    origin: str,
    started_at: datetime,
    finished_at: datetime,
    compensates_id: int | None = None,
    policy_decision: str | None = None,
) -> int:
    """Write the executions audit row (one writer - the schema lands in
    exactly one place) and return its id. Flushes so the id is assigned;
    the caller's session owns the commit.

    `compensates_id` marks a 1.10 compensation row (the executions row it
    undoes); `policy_decision` overrides the recorded verdict string for
    executor-side operations that never went through `policy.decide`
    (compensations record "compensation", not a fabricated allow)."""
    row = models.Execution(
        incident_id=incident_pk,
        candidate_id=latest_selected_candidate_id(session, incident_pk),
        approval_id=approval.id if approval else None,
        compensates_id=compensates_id,
        # idempotency_key is model-computed from these fields - one
        # formula lives in state/models.py, never duplicated here.
        attempt=attempt,
        action=verdict.action,
        params=verdict.params,
        fingerprint=verdict.fingerprint,
        policy_decision=policy_decision or verdict.decision.value,
        verification_required=verdict.verification_required,
        decided_by=approval.decided_by if approval else None,
        origin=origin,
        result=result,
        detail=detail,
        output=output,
        started_at=started_at,
        finished_at=finished_at,
    )
    session.add(row)
    session.flush()
    return row.id


def enforce_execution_gate(
    ctx: ExecutionContext, spec: ActionSpec, params: BaseModel
) -> tuple[PolicyVerdict, models.Approval | None]:
    """Re-run the policy decision inside the executor and, when the
    verdict is REQUIRE_APPROVAL, require a granted approvals row matching
    this exact fingerprint. Returns (verdict, approval); raises
    ExecutionRefusedError before any write when the gate is not satisfied.

    Called by the execute node (which needs the verdict + approval row to
    write the executions audit record) AND again inside each family
    executor - so an executor invoked around the node is still gated."""
    normalized = params.model_dump(mode="json")
    verdict = decide(spec.name, normalized, ctx.environment)
    if verdict.decision == PolicyDecision.DENY:
        raise ExecutionRefusedError(f"policy denied {spec.name}: {verdict.reason}")
    if verdict.decision == PolicyDecision.REQUIRE_APPROVAL:
        approval = _granted_approval(ctx.session, ctx.incident_pk, verdict.fingerprint)
        if approval is None:
            raise ExecutionRefusedError(
                f"{spec.name} requires approval but no granted approvals row exists "
                f"for this incident/fingerprint"
            )
        return verdict, approval
    return verdict, None


# --- Registry --------------------------------------------------------------
#
# Keyed by the catalogue `executor` field, the same way `agent/validation`
# keys validators. Registered at import; `executor_for` raises on an
# unmapped key so a catalogue action can never silently lose its executor.

ExecutorFn = Callable[[ExecutionContext, ActionSpec, BaseModel], ExecutionOutcome]


def _load_registry() -> dict[str, ExecutorFn]:
    from agent.execution import github, jenkins, kubernetes

    return {
        "kubernetes": kubernetes.execute,
        "github": github.execute,
        "jenkins": jenkins.execute,
    }


def executor_for(spec: ActionSpec) -> ExecutorFn:
    """The executor for a catalogue action. Raises ExecutionRefusedError
    on an unregistered executor key - same fail-closed rule as
    `validator_for`."""
    executor = _load_registry().get(spec.executor)
    if executor is None:
        raise ExecutionRefusedError(f"{spec.name}: no executor registered for {spec.executor!r}")
    return executor
