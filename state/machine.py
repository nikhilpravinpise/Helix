"""Incident state machine: the Phase 1 status enum and its allowed edges.

Every status change goes through `transition()`, which validates the edge,
writes a `transitions` row (from, to, cause, timestamp) and updates the
incident's status in the same transaction - a transition can never exist
in memory without being persisted. `record_creation()` writes the initial
NULL -> RECEIVED row so the transitions table alone reconstructs an
incident's full timeline.

Scope note: subphases 1.x below are the Phase 1 recovery machine. Phase 2
(2.1) adds the CONTEXT.md section 14 change-review lifecycle - TRIAGED,
OWNER_ASSIGNED, REVIEW_PENDING, REJECTED, FIX_REQUIRED, APPROVED,
DEPLOYING - as a second lifecycle an incident can follow, selected by
`Incident.kind` (state/models.py): `recovery` incidents (a failure signal)
use the Phase 1 chain unchanged; `change` incidents (a PR/push event) use
the Phase 2 chain below. The two chains meet where CONTEXT.md section 16
says they must: a failed deploy (`DEPLOYING -> FAILED`) re-enters the
Phase 1 recovery loop through the SAME `FAILED -> DIAGNOSING` edge a
recovery incident's exhausted verification uses, and a diagnosed-and-
recovered rollback closes the change incident as RECOVERED too - one
terminal vocabulary for both lifecycles, not two. The section 14 example's
`EXECUTING -> VALIDATING -> RECOVERED` tail refers to this same handoff in
prose; the machine models it as the existing Phase 1 tail
(EXECUTING -> VERIFYING -> RECOVERED) rather than inventing a duplicate
VALIDATING-after-EXECUTING status, per the state-machine doc's original
reasoning.
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

    # Phase 2 (2.1) - the change-review lifecycle, CONTEXT.md section 14.
    # Selected by Incident.kind == "change" (a PR/push event); a
    # `recovery` incident never enters these.
    TRIAGED = "triaged"
    OWNER_ASSIGNED = "owner_assigned"
    REVIEW_PENDING = "review_pending"
    REJECTED = "rejected"
    FIX_REQUIRED = "fix_required"
    APPROVED = "approved"
    DEPLOYING = "deploying"


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
        {
            IncidentStatus.DIAGNOSING,
            # Phase 2 (2.1): a `change` incident (PR/push event) enters the
            # review lifecycle instead of the recovery loop.
            IncidentStatus.TRIAGED,
            IncidentStatus.ESCALATED,
            IncidentStatus.FAILED,
        }
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
            # A refused execution (the executor's own gate said no before
            # any transport call) replans in run - the same edge failed
            # validation already uses, applied to the candidate-level
            # refusal rather than the transport-level error (which still
            # goes to failed and re-enters through diagnosing).
            IncidentStatus.REMEDIATION_PENDING,
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
    # The 1.10 retry budget bounds that loop - exhaustion escalates.
    IncidentStatus.FAILED: frozenset({IncidentStatus.DIAGNOSING, IncidentStatus.ESCALATED}),
    # Phase 2 (2.1) - the change-review lifecycle. RECEIVED gains a second
    # entry edge: a change incident goes RECEIVED -> TRIAGED (a recovery
    # incident still goes -> DIAGNOSING, above).
    IncidentStatus.TRIAGED: frozenset(
        {
            IncidentStatus.OWNER_ASSIGNED,
            # 2.5: ownership genuinely unknown - escalate rather than guess.
            IncidentStatus.ESCALATED,
            IncidentStatus.FAILED,
        }
    ),
    IncidentStatus.OWNER_ASSIGNED: frozenset(
        {IncidentStatus.REVIEW_PENDING, IncidentStatus.ESCALATED, IncidentStatus.FAILED}
    ),
    IncidentStatus.REVIEW_PENDING: frozenset(
        {
            IncidentStatus.APPROVED,
            IncidentStatus.REJECTED,
            IncidentStatus.ESCALATED,
            IncidentStatus.FAILED,
        }
    ),
    IncidentStatus.REJECTED: frozenset(
        {IncidentStatus.FIX_REQUIRED, IncidentStatus.ESCALATED, IncidentStatus.FAILED}
    ),
    # CONTEXT.md section 14: REJECTED -> FIX_REQUIRED -> REVIEW_PENDING -
    # the fix goes back through review, not straight to APPROVED.
    IncidentStatus.FIX_REQUIRED: frozenset(
        {IncidentStatus.REVIEW_PENDING, IncidentStatus.ESCALATED, IncidentStatus.FAILED}
    ),
    IncidentStatus.APPROVED: frozenset(
        {IncidentStatus.DEPLOYING, IncidentStatus.ESCALATED, IncidentStatus.FAILED}
    ),
    IncidentStatus.DEPLOYING: frozenset(
        {
            # A clean deploy closes the change incident.
            IncidentStatus.RECOVERED,
            # A failed deploy hands off to the Phase 1 recovery loop
            # (CONTEXT.md section 16): the SAME DIAGNOSING a recovery
            # incident's FAILED re-enters through, so a rolled-back
            # deployment and a remediated one converge on one vocabulary.
            IncidentStatus.DIAGNOSING,
            IncidentStatus.ESCALATED,
            IncidentStatus.FAILED,
        }
    ),
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
    session.flush()  # the event id keys on the durable transition row
    # Helix's own lifecycle event, written in this same transaction so the
    # event and the state change it describes can never disagree (E5).
    # Imported here rather than at module scope: events.lifecycle needs
    # IncidentStatus from this module.
    from events.lifecycle import emit_for_transition

    emit_for_transition(
        incident,
        from_status=from_status,
        to_status=to_status,
        cause=cause,
        session=session,
        transition_id=row.id,
    )
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
    session.flush()  # the event id keys on the durable transition row
    from events.lifecycle import emit_for_transition

    emit_for_transition(
        incident,
        from_status=None,
        to_status=IncidentStatus(incident.status),
        cause=cause,
        session=session,
        transition_id=row.id,
    )
    session.flush()
    return row
