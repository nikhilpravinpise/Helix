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
retry-budget machinery is 1.10). PASS and NOT_VALIDATABLE produce no
transition - `validating` is a resumable non-terminal status and the 1.8
policy/execution node will pick the run up from here. A run that reaches
`awaiting_approval` ends suspended - the checkpoint holds state until
something resumes it (1.8, D13).
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from agent.state import WorkflowState
from helix.observability import trace_workflow_step
from state import models
from state.machine import IncidentStatus, transition
from state.models import ValidationOutcome

if TYPE_CHECKING:
    from agent.graph import GraphDeps


#: Validation passes allowed per run before escalation - the single
#: retry hook (1.7): one failed candidate may be replanned once; a second
#: consecutive FAIL exhausts the hook (full retry-budget machinery: 1.10).
MAX_VALIDATION_ATTEMPTS = 2


@dataclass(frozen=True)
class _Decision:
    """What the coordinator does this pass: optionally one transition."""

    to_status: IncidentStatus | None
    cause: str
    bump_attempt: bool = False  # failed -> diagnosing re-entry counts an attempt
    replan: bool = False  # validation failed: re-enter planning, plan outputs cleared


def _decide(state: WorkflowState) -> _Decision:
    """The Phase 1 routing policy, keyed on the persisted status.

    Status values come from the incident row (authoritative), so this is
    pure over (status, state) and easy to extend as later subphases wire
    their hops.
    """
    status = state.get("status")
    if status == IncidentStatus.RECEIVED:
        return _Decision(IncidentStatus.DIAGNOSING, "coordinator: begin diagnosis")
    if status == IncidentStatus.FAILED:
        # The failed -> diagnosing re-entry edge: a failed run may be
        # rediagnosed by a later `helix run`.
        return _Decision(
            IncidentStatus.DIAGNOSING,
            "coordinator: rediagnosis after failed run",
            bump_attempt=True,
        )
    if status == IncidentStatus.DIAGNOSING:
        if not state.get("diagnosis_complete"):
            # Diagnosis has not run yet in this state (fresh arrival, or a
            # run resuming after a crash mid-diagnosis) - let it run.
            return _Decision(None, "")
        if state.get("hypothesis"):
            return _Decision(IncidentStatus.REMEDIATION_PENDING, "diagnosis produced a hypothesis")
        return _Decision(IncidentStatus.FAILED, "diagnosis produced no hypothesis")
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
        # pass / not_validatable: no transition. NOT_VALIDATABLE is NOT a
        # pass - it means "proceed to the policy/execution decision
        # unchecked", recorded distinctly so provenance can tell the two
        # apart. `validating` stays the (resumable) status; the 1.8 node
        # picks up from here.
        return _Decision(None, "")
    # Later statuses (awaiting_approval, executing, verifying) route with
    # policy/execution (1.8) and verification (1.9).
    return _Decision(None, "")


def _load_incident(session, incident_id: str) -> models.Incident | None:
    return session.scalar(select(models.Incident).where(models.Incident.incident_id == incident_id))


def coordinator_node(deps: "GraphDeps"):
    """The coordinator graph node. Applies `_decide` through the state
    machine and mirrors the resulting status back into WorkflowState."""

    def _coordinator(state: WorkflowState) -> dict:
        with trace_workflow_step("coordinator"):
            decision = _decide(state)
            with deps.session_scope() as session:
                incident = _load_incident(session, state["incident_id"])
                if incident is None:
                    return {
                        "errors": [
                            *state.get("errors", []),
                            f"coordinator: no such incident {state['incident_id']}",
                        ]
                    }
                update: dict = {}
                if decision.bump_attempt:
                    incident.attempt_count += 1
                    update["attempts"] = incident.attempt_count
                    # Clear run-scoped outputs so a stale hypothesis cannot
                    # survive into the rediagnosis.
                    update["hypothesis"] = None
                    update["diagnosis_complete"] = False
                if decision.replan:
                    # Validation failed: the next remediation_pending pass
                    # must route to the planner, not reuse the failed
                    # selection or its verdict.
                    update["plan_complete"] = False
                    update["selected_action"] = None
                    update["validation"] = None
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
#: replaces the LLM nodes with deterministic rule tables (1.11).
_DIAGNOSIS_NODE = {"E": "diagnosis", "B": "diagnose_and_plan", "A": "rules_diagnosis"}

#: Per-arm node key behind `remediation_pending` (1.6): E routes to the
#: real planner, A to its rules variant (1.11 slot), B has no separate
#: planning step - its fused call lands in 1.11.
_PLAN_NODE = {"E": "plan", "A": "rules_plan", "B": None}

#: Per-arm node key behind `validating` (1.7): E and A share the real
#: validate node; B has no validate step at all.
_VALIDATE_NODE = {"E": "validate", "A": "validate", "B": None}


def make_router(arm: str):
    """Conditional edge out of the coordinator: status -> next node key.

    Three hops carry work: `diagnosing` -> the arm's diagnosis node,
    `remediation_pending` -> the arm's plan node (while the planner has
    not yet reported), and `validating` -> the arm's validate node (while
    no verdict is in state). Everything else ends the run: `validating`
    with a verdict waits for the 1.8 policy/execution node,
    `escalated`/`failed` are terminal outcomes."""
    diagnosis_key = _DIAGNOSIS_NODE[arm]
    plan_key = _PLAN_NODE[arm]
    validate_key = _VALIDATE_NODE[arm]

    def _route(state: WorkflowState) -> str:
        from langgraph.graph import END

        if state.get("status") == IncidentStatus.DIAGNOSING:
            return diagnosis_key
        if state.get("status") == IncidentStatus.REMEDIATION_PENDING:
            return plan_key or END
        if state.get("status") == IncidentStatus.VALIDATING and not state.get("validation"):
            return validate_key or END
        return END

    return _route
