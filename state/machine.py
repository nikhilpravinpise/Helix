"""Incident state machine: the Phase 1 status enum and its allowed edges.

Every status change goes through `transition()`, which validates the edge,
writes a `transitions` row (from, to, cause, timestamp) and updates the
incident's status in the same transaction - a transition can never exist
in memory without being persisted. `record_creation()` writes the initial
NULL -> RECEIVED row so the transitions table alone reconstructs an
incident's full timeline.

Scope note: this is the Phase 1 machine only. CONTEXT.md section 14's
lifecycle statuses (TRIAGED, OWNER_ASSIGNED, REVIEW_PENDING, REJECTED,
FIX_REQUIRED, APPROVED, DEPLOYING) belong to the Phase 2 review flow and
are deliberately absent - see docs/architecture/state-machine.md. The
section 14 example's `EXECUTING -> VALIDATING -> RECOVERED` tail refers to
post-deploy review validation; Phase 1 validates pre-execution
(plan.md 1.7) and verifies post-execution (1.9), hence VERIFYING.
"""

from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:  # avoid the import cycle; models.py imports this module
    from state.models import Incident, Transition


class IncidentStatus(StrEnum):
    """Phase 1 incident lifecycle. Serialized values are lowercase so the
    stored form matches CONTEXT.md section 13's `"awaiting_approval"` shape."""

    RECEIVED = "received"
    DIAGNOSING = "diagnosing"
    REMEDIATION_PENDING = "remediation_pending"
    VALIDATING = "validating"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERED = "recovered"
    ESCALATED = "escalated"
    FAILED = "failed"


#: Statuses an incident never leaves (for dedup and read APIs). FAILED is
#: terminal for the *run* - the FAILED -> DIAGNOSING re-entry edge below
#: exists so a failed run can be rediagnosed, but the incident is still
#: closed for signal-attachment purposes.
TERMINAL_STATUSES: frozenset[IncidentStatus] = frozenset(
    {IncidentStatus.RECOVERED, IncidentStatus.ESCALATED, IncidentStatus.FAILED}
)

#: The Phase 1 edge map. Composition, per the subphase spec: the listed
#: workflow edges, plus a blanket "any non-terminal -> ESCALATED | FAILED"
#: (an investigation can be abandoned or give up from anywhere), plus the
#: single FAILED -> DIAGNOSING re-entry for rediagnosis after a failed run.
ALLOWED_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.RECEIVED: frozenset(
        {IncidentStatus.DIAGNOSING, IncidentStatus.ESCALATED, IncidentStatus.FAILED}
    ),
    IncidentStatus.DIAGNOSING: frozenset(
        {IncidentStatus.REMEDIATION_PENDING, IncidentStatus.ESCALATED, IncidentStatus.FAILED}
    ),
    IncidentStatus.REMEDIATION_PENDING: frozenset(
        {IncidentStatus.VALIDATING, IncidentStatus.ESCALATED, IncidentStatus.FAILED}
    ),
    IncidentStatus.VALIDATING: frozenset(
        {
            IncidentStatus.AWAITING_APPROVAL,
            IncidentStatus.EXECUTING,
            # Failed validation replans (CONTEXT.md section 14: "failed
            # validation should cause replanning"); wired in 1.7.
            IncidentStatus.REMEDIATION_PENDING,
            IncidentStatus.ESCALATED,
            IncidentStatus.FAILED,
        }
    ),
    IncidentStatus.AWAITING_APPROVAL: frozenset(
        {
            IncidentStatus.EXECUTING,
            IncidentStatus.REMEDIATION_PENDING,
            IncidentStatus.ESCALATED,
            IncidentStatus.FAILED,
        }
    ),
    IncidentStatus.EXECUTING: frozenset(
        {
            IncidentStatus.VERIFYING,
            IncidentStatus.DIAGNOSING,
            IncidentStatus.ESCALATED,
            IncidentStatus.FAILED,
        }
    ),
    IncidentStatus.VERIFYING: frozenset(
        {
            IncidentStatus.RECOVERED,
            IncidentStatus.REMEDIATION_PENDING,
            IncidentStatus.ESCALATED,
            IncidentStatus.FAILED,
        }
    ),
    IncidentStatus.RECOVERED: frozenset(),
    IncidentStatus.ESCALATED: frozenset(),
    # FAILED is terminal but re-enterable: a failed run may be rediagnosed.
    IncidentStatus.FAILED: frozenset({IncidentStatus.DIAGNOSING}),
}


class InvalidTransitionError(ValueError):
    """The requested status change is not an allowed edge."""


def is_allowed(from_status: IncidentStatus, to_status: IncidentStatus) -> bool:
    """Whether `from_status -> to_status` is an allowed edge."""
    return to_status in ALLOWED_TRANSITIONS[IncidentStatus(from_status)]


def transition(
    incident: "Incident",
    to_status: IncidentStatus | str,
    *,
    cause: str,
    session: Session,
) -> "Transition":
    """Move `incident` to `to_status`, persisting the transition.

    Validates the edge, inserts the `transitions` row and updates
    `incident.status` + `updated_at` with one flush - the row and the
    status update commit or roll back together in the caller's
    transaction. `transition()` never commits; callers own the
    transaction boundary (see `state.db.session_scope`).

    Raises InvalidTransitionError if the edge is not allowed. The incident
    must be attached to `session`.
    """
    from state.models import Transition

    to_status = IncidentStatus(to_status)
    from_status = IncidentStatus(incident.status)
    if not is_allowed(from_status, to_status):
        raise InvalidTransitionError(
            f"{incident.incident_id}: {from_status} -> {to_status} is not an allowed transition"
        )
    row = Transition(
        incident_id=incident.id,
        from_status=from_status,
        to_status=to_status,
        cause=cause,
    )
    incident.status = to_status
    session.add(row)
    session.flush()
    return row


def record_creation(incident: "Incident", *, cause: str, session: Session) -> "Transition":
    """Persist the incident's entry into RECEIVED as a transitions row with
    NULL from_status, so the table holds the complete timeline including
    the trigger. Not a validated edge - intake calls this once at insert."""
    from state.models import Transition

    row = Transition(
        incident_id=incident.id,
        from_status=None,
        to_status=IncidentStatus(incident.status),
        cause=cause,
    )
    session.add(row)
    session.flush()
    return row
