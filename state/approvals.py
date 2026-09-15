"""Approval-gate decisions (subphase 1.8): the shared write path the CLI
(`helix approve` / `helix reject`) and the API endpoints both use.

An approvals row is created `pending` by the coordinator when policy
requires a human decision; deciding it flips `status` to
`approved`/`rejected` and stamps `decided_by`/`decided_at`/`note`. The
decision is durable and process-independent - any process that can reach
the incident store can grant or deny, and `run_incident` resumes the
suspended workflow from the approvals row + the persisted status.

Duplicate decisions are refused cleanly: `record_decision` raises
`ApprovalDecidedError` on an already-decided row, so a second approve can
never re-fire the gated write (the run row guard plus the executor's own
gate are the other two layers).
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from state import models


class ApprovalDecidedError(RuntimeError):
    """The approvals row is already decided - a duplicate approve/reject
    is a clean error, never a second execution."""


def pending_for_incident(session: Session, incident_pk: int) -> models.Approval | None:
    """The single pending approvals row for an incident, if any - the
    ux_approvals_one_pending index guarantees at most one exists."""
    return session.scalar(
        select(models.Approval)
        .where(models.Approval.incident_id == incident_pk)
        .where(models.Approval.status == models.ApprovalStatus.PENDING)
        .order_by(models.Approval.id.desc())
        .limit(1)
    )


def latest_for_incident(session: Session, incident_pk: int) -> models.Approval | None:
    """The newest approvals row for an incident, any status."""
    return session.scalar(
        select(models.Approval)
        .where(models.Approval.incident_id == incident_pk)
        .order_by(models.Approval.id.desc())
        .limit(1)
    )


def record_decision(
    session: Session,
    approval: models.Approval,
    approved: bool,
    actor: str,
    note: str = "",
) -> models.Approval:
    """Record the human decision on a pending approvals row.

    Flushes but never commits - the caller owns the transaction boundary.
    Raises ApprovalDecidedError when the row is already decided."""
    if approval.status != models.ApprovalStatus.PENDING:
        raise ApprovalDecidedError(
            f"approval {approval.id} is already {approval.status.value}"
            + (f" by {approval.decided_by}" if approval.decided_by else "")
        )
    approval.status = models.ApprovalStatus.APPROVED if approved else models.ApprovalStatus.REJECTED
    approval.decided_by = actor
    approval.decided_at = datetime.now(UTC)
    approval.note = note or None
    session.flush()
    return approval
