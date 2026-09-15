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

For 1.5 the honest end state is REMEDIATION_PENDING: a recorded
hypothesis ends the run (planner/validate/execute/verify routing lands
with those nodes in 1.6-1.9). A run that reaches `awaiting_approval`
ends suspended - the checkpoint holds state until something resumes it
(1.8, D13).
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from agent.state import WorkflowState
from helix.observability import trace_workflow_step
from state import models
from state.machine import IncidentStatus, transition

if TYPE_CHECKING:
    from agent.graph import GraphDeps


@dataclass(frozen=True)
class _Decision:
    """What the coordinator does this pass: optionally one transition."""

    to_status: IncidentStatus | None
    cause: str
    bump_attempt: bool = False  # failed -> diagnosing re-entry counts an attempt


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
    # remediation_pending and every later status: no move for now. Their
    # routing lands with the planner (1.6), validation (1.7), policy and
    # execution (1.8), and verification (1.9).
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


def make_router(arm: str):
    """Conditional edge out of the coordinator: status -> next node key.

    Deliberately tiny in 1.5 - `diagnosing` is the only status that still
    has work to do; everything else ends the run (remediation_pending is
    the honest end state until the planner lands)."""
    diagnosis_key = _DIAGNOSIS_NODE[arm]

    def _route(state: WorkflowState) -> str:
        from langgraph.graph import END

        if state.get("status") == IncidentStatus.DIAGNOSING:
            return diagnosis_key
        return END

    return _route
