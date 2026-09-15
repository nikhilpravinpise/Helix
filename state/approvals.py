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
from state.machine import IncidentStatus, transition


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


def _pending_age(approval: models.Approval, now: datetime) -> float | None:
    """Seconds the pending row has been waiting, or None if unknown
    (created_at can be absent on a not-yet-flushed row)."""
    created = approval.created_at
    if created is None:
        return None
    if created.tzinfo is None:  # SQLite round-trips naive datetimes
        created = created.replace(tzinfo=UTC)
    return (now - created).total_seconds()


def record_decision(
    session: Session,
    approval: models.Approval,
    approved: bool,
    actor: str,
    note: str = "",
    *,
    timeout_s: float | None = None,
) -> models.Approval:
    """Record the human decision on a pending approvals row.

    Flushes but never commits - the caller owns the transaction boundary.
    Raises ApprovalDecidedError when the row is already decided - or,
    subphase 1.10, when the pending row has already outlived
    `timeout_s` (HELIX_APPROVAL_TIMEOUT): a decision arriving after the
    window is refused so a stale approval can never execute "as if still
    pending". The refusal writes nothing - the runner's sweep owns
    marking the row and escalating the incident the next time a run is
    driven."""
    if approval.status != models.ApprovalStatus.PENDING:
        raise ApprovalDecidedError(
            f"approval {approval.id} is already {approval.status.value}"
            + (f" by {approval.decided_by}" if approval.decided_by else "")
        )
    if timeout_s is None:
        from helix.config import get_settings

        timeout_s = get_settings().approval_timeout
    now = datetime.now(UTC)
    age = _pending_age(approval, now)
    if timeout_s > 0 and age is not None and age > timeout_s:
        raise ApprovalDecidedError(
            f"approval {approval.id} timed out: pending for {int(age)}s exceeds "
            f"HELIX_APPROVAL_TIMEOUT={int(timeout_s)}s - refusing the decision; "
            "the next run escalates the incident"
        )
    approval.status = models.ApprovalStatus.APPROVED if approved else models.ApprovalStatus.REJECTED
    approval.decided_by = actor
    approval.decided_at = now
    approval.note = note or None
    session.flush()
    return approval


def sweep_approval_timeout(
    session: Session,
    incident: models.Incident,
    timeout_s: float,
    *,
    now: datetime | None = None,
) -> models.Approval | None:
    """HELIX_APPROVAL_TIMEOUT enforcement (subphase 1.10): if the incident
    sits at `awaiting_approval` on a pending row older than `timeout_s`
    seconds, record the row as rejected-by-timeout and escalate the
    incident in the same transaction. Returns the timed-out row, or None
    when nothing timed out.

    Called by `run_incident` while holding the incident row lock - the
    sweep and the run's resume/insert are serialized per incident, so a
    timed-out approval can never be resumed as if still pending. The
    rejection is recorded on the row itself (decided_by="helix", note
    carries the age) so the timeout is auditable in the approvals table
    and in provenance, and so a later `helix approve` gets a clean
    already-decided conflict instead of silently firing the gated write.
    `timeout_s <= 0` disables the sweep."""
    if timeout_s <= 0 or incident.status != IncidentStatus.AWAITING_APPROVAL:
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
    pending.note = (
        f"approval timeout: pending for {int(age)}s exceeds "
        f"HELIX_APPROVAL_TIMEOUT={int(timeout_s)}s"
    )
    transition(
        incident,
        IncidentStatus.ESCALATED,
        cause=(
            f"approval timeout: approval #{pending.id} for {pending.action} pending "
            f"{int(age)}s exceeds HELIX_APPROVAL_TIMEOUT={int(timeout_s)}s"
        ),
        session=session,
    )
    session.flush()
    return pending
