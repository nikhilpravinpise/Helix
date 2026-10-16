"""Review-gate decisions (subphase 2.6): the shared write path `helix
review approve|reject` and the review API endpoints both use.

Mirrors `state/approvals.py` exactly - a `review_requests` row is created
`pending` by the coordinator when the Review Agent resolves a reviewer;
deciding it flips `status` to `approved`/`rejected` and stamps
`decided_by`/`decided_at`/`note`. The decision is durable and
process-independent for the same reason the approval gate's is: Slack
(2.2) is a delivery surface for the request and the decision alike, never
the record of it.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from state import models
from state.machine import IncidentStatus, transition


class ReviewDecidedError(RuntimeError):
    """The review_requests row is already decided - a duplicate
    approve/reject is a clean error."""


class ReviewReassignError(RuntimeError):
    """A pending review cannot be handed to that person - the message says
    why in plain English."""


class ReviewReassignForbiddenError(ReviewReassignError):
    """The actor is not an owner or admin, so may not reassign at all."""


REASSIGNER_ROLES = ("owner", "admin")


def pending_for_incident(session: Session, incident_pk: int) -> models.ReviewRequest | None:
    """The single pending review request for an incident, if any -
    ux_review_requests_one_pending guarantees at most one exists."""
    return session.scalar(
        select(models.ReviewRequest)
        .where(models.ReviewRequest.incident_id == incident_pk)
        .where(models.ReviewRequest.status == models.ApprovalStatus.PENDING)
        .order_by(models.ReviewRequest.id.desc())
        .limit(1)
    )


def latest_for_incident(session: Session, incident_pk: int) -> models.ReviewRequest | None:
    """The newest review request for an incident, any status."""
    return session.scalar(
        select(models.ReviewRequest)
        .where(models.ReviewRequest.incident_id == incident_pk)
        .order_by(models.ReviewRequest.id.desc())
        .limit(1)
    )


def _pending_age(review: models.ReviewRequest, now: datetime) -> float | None:
    created = review.created_at
    if created is None:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (now - created).total_seconds()


def record_decision(
    session: Session,
    review: models.ReviewRequest,
    approved: bool,
    actor: str,
    note: str = "",
    *,
    timeout_s: float | None = None,
    self_approve: bool = False,
) -> models.ReviewRequest:
    """Record a reviewer's decision on a pending review request.

    Flushes but never commits. Raises ReviewDecidedError on an
    already-decided row, or on a pending row that has already outlived
    `timeout_s` - the same stale-decision refusal 1.10's approval gate
    uses, so a decision cannot fire "as if still pending" after the
    incident has moved on."""
    if review.status != models.ApprovalStatus.PENDING:
        raise ReviewDecidedError(
            f"review {review.id} is already {review.status.value}"
            + (f" by {review.decided_by}" if review.decided_by else "")
        )
    if timeout_s is None:
        from helix.config import get_settings

        timeout_s = get_settings().approval_timeout
    now = datetime.now(UTC)
    age = _pending_age(review, now)
    if timeout_s > 0 and age is not None and age > timeout_s:
        raise ReviewDecidedError(
            f"review {review.id} timed out: pending for {int(age)}s exceeds "
            f"the review timeout of {int(timeout_s)}s - refusing the decision; "
            "the next run escalates the incident"
        )
    from state.deciders import review_refusal

    refusal = review_refusal(review, actor, self_approve=self_approve)
    if refusal is not None:
        raise ReviewDecidedError(f"review {review.id}: {refusal}")
    review.status = models.ApprovalStatus.APPROVED if approved else models.ApprovalStatus.REJECTED
    review.decided_by = actor
    review.decided_at = now
    review.note = note or None
    session.flush()
    return review


def reassign(
    session: Session,
    review: models.ReviewRequest,
    new_reviewer: str,
    actor: str,
    *,
    actor_role: str | None,
) -> models.ReviewRequest:
    """Hand a pending review to another person on the team (decided
    2026-10-16). Only an owner or admin may; `actor_role` None is sign-in
    off, trusted like everywhere else. The new reviewer must be on the team
    (a pending invitee is not yet), must not be the change's author and
    must not already be the reviewer. Who may DECIDE is unchanged - after
    this the new reviewer decides it, and the old one is refused.

    Flushes but never commits. The reassignment is recorded in the
    rationale, and the new reviewer is pinged through the same Slack
    review_request the automatic pick uses."""
    if actor_role is not None and actor_role not in REASSIGNER_ROLES:
        raise ReviewReassignForbiddenError("only an owner or admin can reassign a review")
    if review.status != models.ApprovalStatus.PENDING:
        raise ReviewReassignError(
            f"review {review.id} is already {review.status.value}, so it cannot be reassigned"
        )
    from agent.ops.registry import load_registry
    from state.deciders import author_of

    if new_reviewer not in load_registry().people:
        raise ReviewReassignError(f"{new_reviewer} is not on the team")
    if new_reviewer == author_of(review.incident):
        raise ReviewReassignError(f"{new_reviewer} wrote this change and cannot review it")
    if new_reviewer == review.reviewer:
        raise ReviewReassignError(f"{new_reviewer} is already the reviewer")
    review.rationale = f"{review.rationale}\nReassigned by {actor} (was {review.reviewer})."
    review.reviewer = new_reviewer
    session.flush()
    from integrations.lifecycle_sync import enqueue_review_request

    enqueue_review_request(session, review.incident, review)
    session.flush()
    return review


def sweep_review_timeout(
    session: Session,
    incident: models.Incident,
    timeout_s: float,
    *,
    now: datetime | None = None,
) -> models.ReviewRequest | None:
    """A pending review request older than `timeout_s` is recorded
    rejected-by-timeout and the incident escalates - the same fail-safe
    the approval gate applies: a stale request never silently resolves as
    approved. `timeout_s <= 0` disables the sweep."""
    if timeout_s <= 0 or incident.status != IncidentStatus.REVIEW_PENDING:
        return None
    pending = pending_for_incident(session, incident.id)
    if pending is None:
        return None
    now = now or datetime.now(UTC)
    age = _pending_age(pending, now)
    if age is None or age <= timeout_s:
        return None
    pending.status = models.ApprovalStatus.REJECTED
    pending.decided_by = "helix"
    pending.decided_at = now
    pending.note = f"review timeout: pending for {int(age)}s exceeds {int(timeout_s)}s"
    transition(
        incident,
        IncidentStatus.ESCALATED,
        cause=(
            f"review timeout: review #{pending.id} requested from {pending.reviewer} "
            f"pending {int(age)}s exceeds the review timeout of {int(timeout_s)}s"
        ),
        session=session,
    )
    session.flush()
    return pending
