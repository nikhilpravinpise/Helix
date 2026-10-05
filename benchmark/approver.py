"""Scripted approver (subphase 1.11, decision D2).

When a run suspends at `awaiting_approval` the benchmark needs a decision
without a human. The semantics are deliberately narrow:

- The pending row is approved ONLY when the scenario declares
  `approval_gated: true` AND the pending action is the scenario's
  `optimal_action` - "the benchmark approves the declared optimal gated
  action".
- Anything else pending is REJECTED: a gated action the arm proposed that
  is not the declared optimal gets declined, which exercises the real
  rejection -> replan path rather than blessing whatever showed up.
- Every decision is recorded through `state.approvals.record_decision`
  as the service's owner (the human-in-the-loop rules allow no one
  else), with "benchmark-approver" in the note - the same code path
  `helix approve` and the API use, never a raw table write.

This module never resumes the run itself - the harness calls `helix run`
again after the decision, exactly like a CLI operator would.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from state import models
from state.approvals import ApprovalDecidedError, pending_for_incident, record_decision

#: The audit identity every benchmark approval decision carries.
APPROVER_ACTOR = "benchmark-approver"


@dataclass(frozen=True)
class ApproverOutcome:
    """What the approver did for one suspended run."""

    outcome: str  # "approved" | "rejected" | "no_pending" | "no_decider" | "error"
    action: str | None = None
    detail: str = ""


def decide_pending(
    session: Session, incident: models.Incident, scenario: dict[str, Any]
) -> ApproverOutcome:
    """Apply D2 to the incident's pending approvals row.

    Approve iff scenario.approval_gated AND pending action ==
    scenario.optimal_action; reject otherwise. `no_pending` means the run
    reported awaiting_approval without a row - a defect the harness
    records rather than papers over."""
    pending = pending_for_incident(session, incident.id)
    if pending is None:
        return ApproverOutcome("no_pending", detail="no pending approvals row")
    # The decision is made as the service's owner, the only person the
    # human-in-the-loop rules let decide (state/deciders.py); the scripted
    # role is kept in the note.
    from state.deciders import approval_deciders

    deciders = approval_deciders(incident)
    if not deciders:
        # Nobody may decide: the incident honestly waits for a human (and
        # escalates on the approval timeout), the same as a real one would.
        return ApproverOutcome(
            "no_decider",
            action=pending.action,
            detail=f"{incident.service} has no registered owner to decide approvals",
        )
    actor = deciders[0]
    expected = bool(scenario.get("approval_gated")) and (
        pending.action == scenario.get("optimal_action")
    )
    try:
        if expected:
            record_decision(
                session,
                pending,
                True,
                actor,
                f"{APPROVER_ACTOR}: D2 gated optimal action for scenario {scenario.get('name')}",
            )
            return ApproverOutcome("approved", action=pending.action)
        record_decision(
            session,
            pending,
            False,
            actor,
            f"{APPROVER_ACTOR}: D2 pending action is not the scenario's declared "
            f"gated optimal (optimal={scenario.get('optimal_action')!r})",
        )
        return ApproverOutcome("rejected", action=pending.action)
    except ApprovalDecidedError as exc:
        return ApproverOutcome("error", action=pending.action, detail=str(exc))


def service_suspended_run(session_scope_fn, incident_id: str, scenario: dict) -> ApproverOutcome:
    """Load the incident inside one transaction and apply decide_pending.
    `session_scope_fn` is a session_scope-shaped callable so the harness
    and tests can point it at any engine."""
    with session_scope_fn() as session:
        incident = session.scalar(
            select(models.Incident).where(models.Incident.incident_id == incident_id)
        )
        if incident is None:
            return ApproverOutcome("error", detail=f"no such incident {incident_id}")
        return decide_pending(session, incident, scenario)
