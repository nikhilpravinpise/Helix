"""Scripted reviewer (subphase 2.9/2.11): the change-flow counterpart of
`benchmark.approver`.

When a change incident parks at `review_pending` the benchmark needs a
reviewer decision without a human. The semantics mirror the approver's:

- The decision comes from the scenario's `review_decision` field - a
  scalar (`approve`/`reject`/`timeout`) applied to every review round, or
  a list applied per round so a reject-then-approve re-review is
  declarable.
- Every decision is recorded through `state.reviews.record_decision` with
  `decided_by` naming the benchmark reviewer actor - the same code path
  `helix review approve|reject` and the API use, never a raw table write.
- `timeout` means the reviewer never answers: no decision is recorded and
  the run stays parked, which is exactly what a real non-response looks
  like in the durable record.

This module never resumes the run itself - the harness calls `helix run`
again after the decision, exactly like `helix review` does for an
operator.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from state import models
from state.reviews import ReviewDecidedError, pending_for_incident, record_decision

#: The audit identity every benchmark review decision carries, unless the
#: scenario names its own `reviewer_actor`.
REVIEWER_ACTOR = "benchmark-reviewer"


@dataclass(frozen=True)
class ReviewerOutcome:
    """What the scripted reviewer did for one parked `review_pending`."""

    outcome: str  # "approved" | "rejected" | "timeout" | "no_pending" | "error"
    reviewer: str | None = None
    detail: str = ""


def decision_for_round(scenario: dict[str, Any], round_index: int) -> str:
    """The scripted decision for review round `round_index` (0-based).

    `review_decision` is either a scalar applied to every round or a list
    applied per round; a list shorter than the round index repeats its
    last entry. Absent means `approve` - the common case.
    """
    declared = scenario.get("review_decision", "approve")
    if isinstance(declared, list):
        return declared[min(round_index, len(declared) - 1)]
    return declared


def decide_pending(
    session: Session,
    incident: models.Incident,
    scenario: dict[str, Any],
    *,
    round_index: int = 0,
) -> ReviewerOutcome:
    """Apply the scripted decision to the incident's pending review row.

    `no_pending` means the run reported review_pending without a durable
    row - a defect the harness records rather than papers over. `timeout`
    deliberately records nothing: the request stays pending, which is what
    a reviewer who never answers looks like in the canonical store.
    """
    pending = pending_for_incident(session, incident.id)
    if pending is None:
        return ReviewerOutcome("no_pending", detail="no pending review_requests row")
    decision = decision_for_round(scenario, round_index)
    if decision == "timeout":
        return ReviewerOutcome(
            "timeout",
            reviewer=pending.reviewer,
            detail="scripted reviewer never answers - the request stays pending",
        )
    actor = scenario.get("reviewer_actor") or REVIEWER_ACTOR
    try:
        record_decision(
            session,
            pending,
            decision == "approve",
            actor,
            f"benchmark review: scenario {scenario.get('name')} round {round_index}",
        )
    except ReviewDecidedError as exc:
        return ReviewerOutcome("error", reviewer=pending.reviewer, detail=str(exc))
    return ReviewerOutcome(
        "approved" if decision == "approve" else "rejected", reviewer=pending.reviewer
    )


def service_suspended_review(
    session_scope_fn, incident_id: str, scenario: dict, *, round_index: int = 0
) -> ReviewerOutcome:
    """Load the incident inside one transaction and apply decide_pending.
    `session_scope_fn` is a session_scope-shaped callable so the harness
    and tests can point it at any engine."""
    with session_scope_fn() as session:
        incident = session.scalar(
            select(models.Incident).where(models.Incident.incident_id == incident_id)
        )
        if incident is None:
            return ReviewerOutcome("error", detail=f"no such incident {incident_id}")
        return decide_pending(session, incident, scenario, round_index=round_index)
