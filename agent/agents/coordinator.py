"""Coordinator: the only place incident status transitions happen.

Two halves:

- `coordinator_node(deps)` - a graph node that reads the incident row,
  decides the next status from `WorkflowState`, and applies it through
  `state.machine.transition()` (which persists a transitions row in the
  same transaction). Worker nodes NEVER touch incident status - they only
  write their outputs into state, and the coordinator translates those
  outputs into transitions here, in one place.
- `make_router(arm)` - the conditional-edge function LangGraph calls
  after the coordinator runs; it maps the just-updated status to the
  next node key. The map is per-arm (D8): each arm's diagnosis variant
  sits behind the `diagnosing` hop.

The 1.6 extension: at `remediation_pending` the router sends the run to
`plan`; when the planner reports back, the coordinator turns its outcome
into a transition - `selected_action` -> VALIDATING, an escalation cause
-> ESCALATED (a real terminal outcome, not an exception), a failed
proposal call -> FAILED.

The 1.7 extension: at `validating` the router sends the run to the real
`validate` node while no verdict is in state; when it reports back, a
FAIL transitions back to `remediation_pending` with the plan outputs
cleared (the single-retry hook - the failed fingerprint is already in
`excluded_candidates` so the planner cannot re-propose it), and a SECOND
consecutive FAIL escalates (the single retry is exhausted; the full
retry-budget machinery is 1.10). PASS and NOT_VALIDATABLE feed the 1.8
policy gate below.

The 1.8 extension: after validation the coordinator computes the policy
verdict itself (`agent.policy.decide`) and routes on it:

- ALLOW / ALLOW_WITH_CHECKS -> `validating -> executing`; the router
  sends the run to the real `execute` node.
- DENY -> `validating -> remediation_pending` replan; the denied
  fingerprint joins the exclusion list (and is audited as a refused
  executions row).
- REQUIRE_APPROVAL -> a pending `approvals` row is created in the same
  transaction as the `validating -> awaiting_approval` transition, and
  the run ends - suspended until `helix approve`/`helix reject` (or the
  API) records the decision and resumes.

At `awaiting_approval` the coordinator reads the approvals row for the
selected fingerprint: approved -> `executing`, rejected ->
`remediation_pending` replan with the fingerprint excluded, still pending
-> the run ends suspended again. At `executing` a success transitions to
`verifying`; an error or refusal transitions to `failed` - rediagnosis is
the `failed -> diagnosing` re-entry edge, and the failed fingerprint is
excluded so the replan cannot blindly repeat it.

The 1.9 extension: at `verifying` the router sends the run to the real
`verify` node while no verification is in state; when it reports back,
`pass` closes the incident as `recovered`, and `fail` routes by the
action's reversibility class - irreversible escalates directly (nothing
can be compensated), everything else goes back to `remediation_pending`
with the failed fingerprint excluded and the `needs_compensation` debt
recorded for the 1.10 compensation hook.

The 1.10 extensions:

- `compensate` node: a `verifying` fail on a compensable action with an
  unpaid debt parks (no transition) and the router sends the run to
  `compensate` first; on its return, a success replans (the debt is
  paid) while a refusal/error escalates - a compensable failure that
  cannot be undone is the human-required case.
- Retry budget: every failed cycle consumes one attempt
  (`incidents.attempt_count`). A `verifying` failure or a `failed`
  re-entry that would exceed `HELIX_MAX_ATTEMPTS` escalates as
  "retry budget exhausted" instead of looping.
- Node failure: a timed-out/crashed worker node writes
  `state["node_failed"]`; the coordinator turns it into a FAILED
  transition (the failed -> diagnosing re-entry then applies the budget
  like any other failure).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from agent.actions.catalogue import Reversibility, get_action
from agent.policy import PolicyDecision, PolicyVerdict, decide
from agent.state import WorkflowState
from agent.validation import candidate_fingerprint
from helix.config import get_settings
from helix.observability import trace_workflow_step
from state import models
from state.machine import IncidentStatus, transition
from state.models import ApprovalStatus, ValidationOutcome, VerificationOutcome

if TYPE_CHECKING:
    from agent.graph import GraphDeps


#: Validation passes allowed per run before escalation - the single
#: retry hook (1.7): one failed candidate may be replanned once; a second
#: consecutive FAIL exhausts the hook (full retry-budget machinery: 1.10).
MAX_VALIDATION_ATTEMPTS = 2


@dataclass(frozen=True)
class _Decision:
    """What the coordinator does this pass: optionally one transition
    plus the bookkeeping that must land in the same transaction."""

    to_status: IncidentStatus | None
    cause: str
    bump_attempt: bool = False  # failed -> diagnosing re-entry counts an attempt
    count_attempt: bool = False  # 1.10 - a failed cycle consumed one attempt
    replan: bool = False  # back to planning: plan outputs cleared
    exclude_fingerprint: str | None = None  # joins state.excluded_candidates
    request_approval: dict[str, Any] | None = None  # pending approvals row to persist
    deny: PolicyVerdict | None = None  # policy DENY: audited as a refused execution
    policy: dict[str, Any] | None = None  # PolicyVerdict snapshot for state["policy"]
    approval: dict[str, Any] | None = None  # approvals-row snapshot for state["approval"]


def _policy_gate(
    state: WorkflowState, approval: models.Approval | None, arm: str = "E"
) -> _Decision:
    """The 1.8 access-table routing for a validated selection. Shared by
    every arm - arm B has no validate node, but a selection still faces
    the same policy decision before any write."""
    selected = state.get("selected_action") or {}
    action = str(selected.get("action") or "")
    verdict = decide(action, selected.get("params") or {}, state.get("environment") or "")
    policy = verdict.to_state()
    if verdict.decision == PolicyDecision.DENY:
        if arm in ("B", "C"):
            # Arm B/C have no replan node - a denied action is terminal.
            return _Decision(
                IncidentStatus.ESCALATED,
                f"policy denied {verdict.action}: {verdict.reason} (arm {arm} has no replan stage)",
                exclude_fingerprint=verdict.fingerprint or None,
                deny=verdict,
                policy=policy,
            )
        return _Decision(
            IncidentStatus.REMEDIATION_PENDING,
            f"policy denied {verdict.action}: {verdict.reason}",
            replan=True,
            exclude_fingerprint=verdict.fingerprint or None,
            deny=verdict,
            policy=policy,
        )
    if verdict.decision == PolicyDecision.REQUIRE_APPROVAL:
        if approval is not None and approval.status == ApprovalStatus.APPROVED:
            return _Decision(
                IncidentStatus.EXECUTING,
                f"approval granted by {approval.decided_by} for {verdict.action}",
                policy=policy,
                approval=_approval_state(approval),
            )
        if approval is not None and approval.status == ApprovalStatus.REJECTED:
            if arm in ("B", "C"):
                return _Decision(
                    IncidentStatus.ESCALATED,
                    f"approval rejected by {approval.decided_by} for {verdict.action} "
                    f"(arm {arm} has no replan stage)",
                    exclude_fingerprint=verdict.fingerprint,
                    policy=policy,
                    approval=_approval_state(approval),
                )
            return _Decision(
                IncidentStatus.REMEDIATION_PENDING,
                f"approval rejected by {approval.decided_by} for {verdict.action}",
                replan=True,
                exclude_fingerprint=verdict.fingerprint,
                policy=policy,
                approval=_approval_state(approval),
            )
        # None or still pending: park the run at awaiting_approval. The
        # coordinator persists the pending row in the same transaction.
        return _Decision(
            IncidentStatus.AWAITING_APPROVAL,
            f"policy requires approval for {verdict.action}: {verdict.reason}",
            request_approval={
                "action": verdict.action,
                "params": verdict.params,
                "fingerprint": verdict.fingerprint,
            },
            policy=policy,
        )
    return _Decision(
        IncidentStatus.EXECUTING,
        f"policy {verdict.decision.value} for {verdict.action} ({verdict.reason})",
        policy=policy,
    )


def _approval_state(approval: models.Approval) -> dict[str, Any]:
    """The approvals-row snapshot carried in state for provenance."""
    return {
        "id": approval.id,
        "action": approval.action,
        "params": approval.params,
        "fingerprint": approval.fingerprint,
        "status": approval.status.value,
        "decided_by": approval.decided_by,
    }


def _decide(
    state: WorkflowState,
    approval: models.Approval | None,
    max_attempts: int | None = None,
    arm: str = "E",
) -> _Decision:
    """The Phase 1 routing policy, keyed on the persisted status.

    Status values come from the incident row (authoritative), so this is
    pure over (status, state, approval) - `approval` is the newest
    approvals row matching the selected candidate's fingerprint, loaded
    by the coordinator node. `max_attempts` is the 1.10 retry budget
    (HELIX_MAX_ATTEMPTS); None falls back to settings so state-level
    callers need not know the wiring.
    """
    budget = max_attempts if max_attempts is not None else get_settings().max_attempts
    status = state.get("status")
    if status == IncidentStatus.RECEIVED:
        return _Decision(IncidentStatus.DIAGNOSING, "coordinator: begin diagnosis")
    if status == IncidentStatus.FAILED:
        # The failed -> diagnosing re-entry edge: a failed run may be
        # rediagnosed by a later `helix run`. The just-failed cycle
        # consumed one attempt; when the budget is spent the incident
        # escalates instead of re-entering forever (1.10).
        consumed = state.get("attempts", 0) + 1
        if consumed >= budget:
            return _Decision(
                IncidentStatus.ESCALATED,
                f"retry budget exhausted: {consumed} attempts consumed "
                f"(HELIX_MAX_ATTEMPTS={budget})",
                count_attempt=True,
            )
        return _Decision(
            IncidentStatus.DIAGNOSING,
            "coordinator: rediagnosis after failed run",
            bump_attempt=True,
        )
    # A worker node that timed out or crashed left `node_failed` in state
    # - the coordinator fails the run through the machine (the failed ->
    # diagnosing re-entry + budget then apply like any other failure).
    node_failed = state.get("node_failed")
    if node_failed:
        return _Decision(
            IncidentStatus.FAILED,
            f"{node_failed.get('node', '?')}: {node_failed.get('cause', 'step timeout')}",
        )
    if status == IncidentStatus.DIAGNOSING:
        if not state.get("diagnosis_complete"):
            # Diagnosis has not run yet in this state (fresh arrival, or a
            # run resuming after a crash mid-diagnosis) - let it run.
            return _Decision(None, "")
        if state.get("hypothesis"):
            return _Decision(IncidentStatus.REMEDIATION_PENDING, "diagnosis produced a hypothesis")
        # Carry WHY. "no hypothesis" is the symptom of several unrelated
        # causes - a provider 404 on a decommissioned model, a rate limit,
        # a schema the model would not satisfy, or a model that genuinely
        # had nothing to say - and they rendered identically in the
        # durable record. Diagnosing two separate benchmark failures meant
        # reproducing the LLM call by hand, because the transition said
        # only that nothing came back. The node already records the reason
        # in state["errors"]; it was simply being dropped here.
        reasons = [str(e) for e in (state.get("errors") or [])]
        detail = f": {reasons[-1]}" if reasons else " and recorded no reason"
        return _Decision(IncidentStatus.FAILED, f"diagnosis produced no hypothesis{detail}")
    if status == IncidentStatus.REMEDIATION_PENDING:
        if state.get("plan_failed"):
            return _Decision(
                IncidentStatus.FAILED,
                f"planner could not produce a plan: {state['plan_failed']}",
            )
        if not state.get("plan_complete"):
            # The planner has not run yet in this status - route to it.
            return _Decision(None, "")
        if state.get("selected_action"):
            action = state["selected_action"].get("action", "?")
            return _Decision(IncidentStatus.VALIDATING, f"planner selected {action}")
        return _Decision(
            IncidentStatus.ESCALATED,
            state.get("escalation_cause") or "planner found no safe action",
        )
    if status == IncidentStatus.VALIDATING:
        validation = state.get("validation")
        if not validation:
            if arm in ("B", "C"):
                # Arm B/C have no validate node (1.11): their fused
                # selection goes straight to the shared policy gate - the
                # access table still applies, the pre-execution check does
                # not.
                if not state.get("selected_action"):
                    return _Decision(IncidentStatus.FAILED, "validating without a selected action")
                return _policy_gate(state, approval, arm)
            # The validate node has not run in this status yet - the
            # router sends the run to it.
            return _Decision(None, "")
        outcome = validation.get("outcome")
        if outcome == ValidationOutcome.FAIL.value:
            action = validation.get("action", "?")
            if state.get("validation_attempts", 0) >= MAX_VALIDATION_ATTEMPTS:
                return _Decision(
                    IncidentStatus.ESCALATED,
                    f"validation failed again for {action} after replan: "
                    f"{validation.get('reason')} (single retry exhausted)",
                )
            return _Decision(
                IncidentStatus.REMEDIATION_PENDING,
                f"validation failed for {action}: {validation.get('reason')}",
                replan=True,
            )
        # pass / not_validatable -> the 1.8 policy gate. NOT_VALIDATABLE
        # is NOT a pass - it means "proceed to the policy/execution
        # decision unchecked", recorded distinctly so provenance can tell
        # the two apart. The gate is what decides executing vs
        # awaiting_approval vs a denied replan.
        if not state.get("selected_action"):
            return _Decision(IncidentStatus.FAILED, "validating without a selected action")
        return _policy_gate(state, approval, arm)
    if status == IncidentStatus.AWAITING_APPROVAL:
        # The run is suspended on a pending approvals row. `approval` is
        # the newest row matching the selected candidate's fingerprint.
        if approval is not None and approval.status == ApprovalStatus.APPROVED:
            return _Decision(
                IncidentStatus.EXECUTING,
                f"approval granted by {approval.decided_by} for {approval.action}",
                approval=_approval_state(approval),
            )
        if approval is not None and approval.status == ApprovalStatus.REJECTED:
            if arm in ("B", "C"):
                # Rejection needs a replan; arm B/C have none - terminal.
                return _Decision(
                    IncidentStatus.ESCALATED,
                    f"approval rejected by {approval.decided_by} for {approval.action} "
                    f"(arm {arm} has no replan stage)",
                    exclude_fingerprint=approval.fingerprint,
                    approval=_approval_state(approval),
                )
            return _Decision(
                IncidentStatus.REMEDIATION_PENDING,
                f"approval rejected by {approval.decided_by} for {approval.action}",
                replan=True,
                exclude_fingerprint=approval.fingerprint,
                approval=_approval_state(approval),
            )
        # Still pending (or the row is missing) - stay suspended; the
        # router ends the run here.
        return _Decision(None, "")
    if status == IncidentStatus.EXECUTING:
        execution = state.get("execution")
        if not execution:
            # The execute node has not run in this status yet - route.
            return _Decision(None, "")
        result = execution.get("result")
        if result == models.ExecutionResult.SUCCESS.value:
            return _Decision(
                IncidentStatus.VERIFYING,
                f"executed {execution.get('action')}: {execution.get('detail')}",
            )
        # error / refused: the run fails here. Rediagnosis is the
        # failed -> diagnosing re-entry on a later run; the fingerprint
        # joins exclusions so a replan cannot blindly repeat it.
        return _Decision(
            IncidentStatus.FAILED,
            f"execution {result} for {execution.get('action')}: {execution.get('detail')}",
            exclude_fingerprint=execution.get("fingerprint"),
        )
    if status == IncidentStatus.VERIFYING:
        verification = state.get("verification")
        if not verification:
            # The verify node has not run in this status yet - route.
            return _Decision(None, "")
        action = str(verification.get("action") or "?")
        if verification.get("outcome") == VerificationOutcome.PASS.value:
            return _Decision(
                IncidentStatus.RECOVERED,
                f"verification passed for {action}: {verification.get('detail', '')}",
            )
        # Reversibility comes from the catalogue, not the state payload -
        # a reloaded verifications row (crash mid-verify) may not carry
        # it, and the routing must not trust a field it could re-derive.
        spec = get_action(action)
        fingerprint = verification.get("fingerprint") or None
        detail = verification.get("detail", "")
        if spec is not None and spec.reversibility == Reversibility.IRREVERSIBLE:
            return _Decision(
                IncidentStatus.ESCALATED,
                f"verification failed for irreversible action {action}: {detail}",
                count_attempt=True,
            )
        if spec is not None and spec.reversibility == Reversibility.COMPENSABLE:
            # The undo runs BEFORE the replan decision (1.10): an unpaid
            # debt with no compensation result yet parks this status so
            # the router sends the run to `compensate`; a failed
            # compensation escalates (a compensable change Helix cannot
            # undo is the human-required case).
            compensation = state.get("compensation")
            if compensation is None and state.get("needs_compensation"):
                return _Decision(None, "")
            if compensation is not None and compensation.get("result") != "success":
                return _Decision(
                    IncidentStatus.ESCALATED,
                    f"compensation for {action} failed: {compensation.get('detail', '')}",
                    count_attempt=True,
                )
            # Compensated (or no debt owed): fall through to the bounded
            # replan below.
        # Stateless or compensated failure: consume one attempt, then
        # replan - or escalate when the retry budget is spent.
        consumed = state.get("attempts", 0) + 1
        if consumed >= budget:
            return _Decision(
                IncidentStatus.ESCALATED,
                f"verification failed for {action}: {detail}; retry budget "
                f"exhausted after {consumed} attempts (HELIX_MAX_ATTEMPTS={budget})",
                count_attempt=True,
                exclude_fingerprint=fingerprint,
            )
        return _Decision(
            IncidentStatus.REMEDIATION_PENDING,
            f"verification failed for {action}: {detail}",
            replan=True,
            exclude_fingerprint=fingerprint,
            count_attempt=True,
        )
    return _Decision(None, "")


def _load_incident(session, incident_id: str) -> models.Incident | None:
    return session.scalar(select(models.Incident).where(models.Incident.incident_id == incident_id))


def _selected_fingerprint(state: WorkflowState) -> str | None:
    """The candidate fingerprint of the in-state selection - the identity
    approvals rows are keyed on."""
    selected = state.get("selected_action") or {}
    action = selected.get("action")
    if not action:
        return None
    return candidate_fingerprint(action, selected.get("params") or {})


def _latest_approval(session, incident_pk: int, fingerprint: str | None) -> models.Approval | None:
    """The newest approvals row for this incident + fingerprint, any
    status - what the coordinator decides from at awaiting_approval and
    the policy gate consults at validating."""
    if not fingerprint:
        return None
    return session.scalar(
        select(models.Approval)
        .where(models.Approval.incident_id == incident_pk)
        .where(models.Approval.fingerprint == fingerprint)
        .order_by(models.Approval.id.desc())
        .limit(1)
    )


def _record_denial(
    session, deps: "GraphDeps", state: WorkflowState, verdict: PolicyVerdict
) -> None:
    """Audit a policy-level DENY as a refused executions row - the
    executions table records what was attempted under which decision, so
    a denied action is as queryable as an executed one."""
    from agent.execution import persist_execution

    now = datetime.now(UTC)
    persist_execution(
        session,
        incident_pk=state["incident_pk"],
        attempt=state.get("attempts", 0),
        verdict=verdict,
        approval=None,
        result=models.ExecutionResult.REFUSED,
        detail=f"policy denied: {verdict.reason}",
        output=None,
        origin=deps.write_origin,
        started_at=now,
        finished_at=now,
    )


def coordinator_node(deps: "GraphDeps", arm: str = "E"):
    """The coordinator graph node. Applies `_decide` through the state
    machine and mirrors the resulting status back into WorkflowState.
    `arm` selects the arm-specific decision behavior (arm B skips the
    absent validate/verify stages and escalates where a replan would be
    needed)."""

    def _coordinator(state: WorkflowState) -> dict:
        with trace_workflow_step("coordinator"):
            with deps.session_scope() as session:
                incident = _load_incident(session, state["incident_id"])
                if incident is None:
                    return {
                        "errors": [
                            *state.get("errors", []),
                            f"coordinator: no such incident {state['incident_id']}",
                        ]
                    }
                approval = _latest_approval(session, incident.id, _selected_fingerprint(state))
                decision = _decide(state, approval, max_attempts=deps.max_attempts, arm=arm)
                update: dict = {}
                if decision.bump_attempt or decision.count_attempt:
                    # A failed cycle consumed one attempt (1.10): the
                    # counter is the persisted retry budget ledger. The
                    # clearing below is only for the failed -> diagnosing
                    # re-entry; replan/escalate edges just count.
                    incident.attempt_count += 1
                    update["attempts"] = incident.attempt_count
                if decision.bump_attempt:
                    # Clear run-scoped outputs so nothing stale survives
                    # into the rediagnosis: a leftover `execution` dict
                    # would let the `executing` branch decide from a
                    # previous attempt's result before the node ran.
                    update["hypothesis"] = None
                    update["diagnosis_complete"] = False
                    update["plan_complete"] = False
                    update["plan_failed"] = None
                    update["escalation_cause"] = None
                    update["selected_action"] = None
                    update["candidates"] = []
                    update["validation"] = None
                    update["validation_attempts"] = 0
                    update["policy"] = None
                    update["approval"] = None
                    update["execution"] = None
                    update["verification"] = None
                    update["compensation"] = None
                    update["node_failed"] = None
                if decision.replan:
                    # Back to planning: the next remediation_pending pass
                    # must route to the planner, not reuse the failed or
                    # rejected selection, its verdict, or any stale
                    # execution/policy/verification snapshot. NOT
                    # `needs_compensation`: an unpaid compensation debt
                    # survives the replan until the compensate node pays
                    # it or escalates.
                    update["plan_complete"] = False
                    update["selected_action"] = None
                    update["validation"] = None
                    update["execution"] = None
                    update["policy"] = None
                    update["verification"] = None
                    update["compensation"] = None
                if decision.exclude_fingerprint:
                    update["excluded_candidates"] = [
                        *state.get("excluded_candidates", []),
                        decision.exclude_fingerprint,
                    ]
                if decision.request_approval is not None:
                    # The gate parks here: persist the pending approvals
                    # row in the same transaction as the transition, so an
                    # incident can never sit at awaiting_approval without
                    # a row to decide. ux_approvals_one_pending means one
                    # pending row per incident - a stale pending row for a
                    # different (replaced) selection is superseded first.
                    req = decision.request_approval
                    pending_other = session.scalar(
                        select(models.Approval)
                        .where(models.Approval.incident_id == incident.id)
                        .where(models.Approval.status == ApprovalStatus.PENDING)
                        .where(models.Approval.fingerprint != req["fingerprint"])
                        .order_by(models.Approval.id.desc())
                        .limit(1)
                    )
                    if pending_other is not None:
                        pending_other.status = ApprovalStatus.REJECTED
                        pending_other.decided_by = "helix"
                        pending_other.decided_at = datetime.now(UTC)
                        pending_other.note = "superseded: the selection it guarded was replaced"
                    if approval is None or approval.status != ApprovalStatus.PENDING:
                        approval = models.Approval(
                            incident_id=incident.id,
                            candidate_id=session.scalar(
                                select(models.Candidate.id)
                                .where(models.Candidate.incident_id == incident.id)
                                .where(models.Candidate.selected.is_(True))
                                .order_by(models.Candidate.id.desc())
                                .limit(1)
                            ),
                            action=req["action"],
                            params=req["params"],
                            fingerprint=req["fingerprint"],
                            status=ApprovalStatus.PENDING,
                        )
                        session.add(approval)
                        session.flush()
                    update["approval"] = _approval_state(approval)
                if decision.deny is not None:
                    _record_denial(session, deps, state, decision.deny)
                if decision.policy is not None:
                    update["policy"] = decision.policy
                if decision.approval is not None:
                    update["approval"] = decision.approval
                if decision.to_status is not None:
                    transition(
                        incident,
                        decision.to_status,
                        cause=decision.cause,
                        session=session,
                    )
                update["status"] = IncidentStatus(incident.status).value
                return update

    return _coordinator


#: Per-arm node key sitting behind the `diagnosing` status (D8). Arm E is
#: the full graph; B collapses diagnosis+planning into one LLM call; A
#: replaces the LLM nodes with deterministic rule tables (1.11). C is B
#: plus retrieval (E6) - identical routing, so it shares B's entry.
_DIAGNOSIS_NODE = {
    "E": "diagnosis",
    "B": "diagnose_and_plan",
    "C": "diagnose_and_plan",
    "A": "rules_diagnosis",
}

#: Per-arm node key behind `remediation_pending` (1.6): E routes to the
#: real planner, A to its rules variant (1.11 slot), B/C have no separate
#: planning step - their fused call lands in 1.11.
_PLAN_NODE = {"E": "plan", "A": "rules_plan", "B": None, "C": None}

#: Per-arm node key behind `validating` (1.7): E and A share the real
#: validate node; B/C have no validate step at all.
_VALIDATE_NODE = {"E": "validate", "A": "validate", "B": None, "C": None}

#: Per-arm node key behind `executing` (1.8): the real execute node is
#: shared by every arm (it is pure code - the policy gate lives in the
#: coordinator's routing plus the executor's own re-check).
_EXECUTE_NODE = {"E": "execute", "A": "execute", "B": "execute", "C": "execute"}

#: Per-arm node key behind `verifying` (1.9): E and A share the real
#: verify node; B/C drop it (its benchmark harness inspects actual system
#: state itself, so the run ends at `verifying` for scoring).
_VERIFY_NODE = {"E": "verify", "A": "verify", "B": None, "C": None}

#: Per-arm node key for the 1.10 compensation hop: parked at `verifying`
#: while a failed compensable action still owes an un-undone change.
_COMPENSATE_NODE = {"E": "compensate", "A": "compensate", "B": None, "C": None}


def make_router(arm: str):
    """Conditional edge out of the coordinator: status -> next node key.

    The hops that carry work: `diagnosing` -> the arm's diagnosis node,
    `remediation_pending` -> the arm's plan node (while the planner has
    not yet reported), `validating` -> the arm's validate node (while no
    verdict is in state), `executing` -> the execute node (while no
    execution result is in state), and `verifying` -> the verify node
    (while no verification is in state) or the compensate node (a failed
    verification whose compensable change is still unpaid, 1.10).
    Everything else ends the run: `awaiting_approval` is the
    suspended-on-approval status (a decided row resumes the run through
    `helix approve`/`reject` or the API), and `recovered`/`escalated`/
    `failed` are terminal outcomes."""
    diagnosis_key = _DIAGNOSIS_NODE[arm]
    plan_key = _PLAN_NODE[arm]
    validate_key = _VALIDATE_NODE[arm]
    execute_key = _EXECUTE_NODE[arm]
    verify_key = _VERIFY_NODE[arm]
    compensate_key = _COMPENSATE_NODE[arm]

    def _route(state: WorkflowState) -> str:
        from langgraph.graph import END

        if state.get("status") == IncidentStatus.DIAGNOSING:
            return diagnosis_key
        if state.get("status") == IncidentStatus.REMEDIATION_PENDING:
            return plan_key or END
        if state.get("status") == IncidentStatus.VALIDATING and not state.get("validation"):
            return validate_key or END
        if state.get("status") == IncidentStatus.EXECUTING and not state.get("execution"):
            return execute_key or END
        if state.get("status") == IncidentStatus.VERIFYING:
            verification = state.get("verification")
            if not verification:
                return verify_key or END
            # Failed verification + unpaid compensation debt + no
            # compensation result yet -> the compensate node runs first;
            # the coordinator re-decides once it reports back.
            if (
                verification.get("outcome") == VerificationOutcome.FAIL.value
                and state.get("needs_compensation")
                and state.get("compensation") is None
            ):
                return compensate_key or END
            return END
        return END

    return _route
