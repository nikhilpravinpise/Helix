"""Which external effects each lifecycle transition commits (2.2/2.3/2.4).

One function, `enqueue_for_transition`, called by the coordinator in the
SAME transaction as `state.machine.transition()`: the IntegrationOutbox
rows are the durable record of what the outside world must be told, so
an outage in Slack/Jira/Notion can never make the canonical state and
its projections disagree about what happened - the drain
(integrations/outbox.py) retries delivery, the transition itself is
already committed truth.

The mapping is deliberately declarative: a transition either creates
follow-up work for a human system or it does not, and that decision
lives in one table a reviewer can audit, not spread through agent code.

Provider names are written at enqueue time from settings because the row
must say WHICH provider the drain will use - the provider can be
reconfigured between enqueue and drain without corrupting intent, and
the work_items dedup key is (incident, provider) anyway.
"""

from typing import Any

from sqlalchemy.orm import Session

from helix.config import get_settings
from state import models
from state.machine import IncidentStatus

#: Transitions that are milestones worth rebuilding the knowledge page
#: for (2.4: "significant state transitions rather than on every event").
#: Intermediate passes-through like `triaged` still get a Slack line;
#: the page projection is reserved for states a human reads the page to
#: understand.
PAGE_SYNC_STATUSES = frozenset(
    {
        IncidentStatus.OWNER_ASSIGNED,
        IncidentStatus.REVIEW_PENDING,
        IncidentStatus.REJECTED,
        IncidentStatus.APPROVED,
        IncidentStatus.DEPLOYING,
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.EXECUTING,
        IncidentStatus.RECOVERED,
        IncidentStatus.ESCALATED,
        IncidentStatus.FAILED,
    }
)

#: Neutral work-item status each lifecycle status maps to, when one
#: applies (CONTEXT.md section 11 vocabulary). Absent = no transition op.
_WORK_STATUS: dict[IncidentStatus, str] = {
    IncidentStatus.OWNER_ASSIGNED: "open",
    IncidentStatus.REVIEW_PENDING: "in_progress",
    IncidentStatus.FIX_REQUIRED: "in_progress",
    IncidentStatus.DEPLOYING: "in_progress",
    IncidentStatus.EXECUTING: "in_progress",
    IncidentStatus.RECOVERED: "done",
    IncidentStatus.ESCALATED: "blocked",
}


def _enqueue(
    session: Session,
    incident: models.Incident,
    integration: str,
    op: str,
    payload: dict[str, Any],
) -> models.IntegrationOutbox:
    row = models.IntegrationOutbox(
        incident_id=incident.id,
        integration=integration,
        op=op,
        payload=payload,
    )
    session.add(row)
    return row


def _work_spec(incident: models.Incident) -> dict[str, Any]:
    """The provider-neutral create payload - the drain turns it into a
    `WorkItemSpec`; the provider maps it onto Jira/Linear/Trello."""
    title = f"{incident.incident_id}: {incident.service} {incident.kind} ({incident.severity})"
    return {
        "incident_id": incident.incident_id,
        "title": title,
        "description": (
            f"Helix incident {incident.incident_id} on {incident.service} "
            f"({incident.environment}), severity {incident.severity}."
        ),
        "service": incident.service,
        "environment": incident.environment,
        "severity": incident.severity,
        "priority": _priority_for(incident.severity),
        "labels": ["helix", f"kind:{incident.kind}"],
    }


def _priority_for(severity: str) -> str:
    return {"critical": "critical", "high": "high", "medium": "medium"}.get(
        str(severity).lower(), "low"
    )


def enqueue_for_transition(
    session: Session,
    incident: models.Incident,
    to_status: IncidentStatus,
    *,
    cause: str = "",
) -> None:
    """Commit the outbox ops `to_status` implies, in the caller's
    transaction. Pure enqueue - no external call is ever made here, so a
    provider outage cannot stall or corrupt the transition itself."""
    settings = get_settings()
    provider = settings.work_provider

    # Slack: every transition is a threaded incident update (2.2).
    _enqueue(
        session,
        incident,
        "slack",
        "notify",
        {"text": f"{incident.incident_id}: -> {to_status.value} - {cause}"},
    )

    # Notion: milestone transitions refresh the incident page (2.4).
    if to_status in PAGE_SYNC_STATUSES:
        _enqueue(session, incident, "notion", "sync_page", {})

    # Work management (2.3): one item per incident, kept in step.
    if to_status in (IncidentStatus.TRIAGED, IncidentStatus.DIAGNOSING):
        # Change incidents start tracking once triage confirms they are
        # real; recovery incidents once diagnosis begins. The work_items
        # unique index makes a second enqueue harmless (drain dedups).
        _enqueue(session, incident, provider, "create_work_item", _work_spec(incident))
    if to_status == IncidentStatus.OWNER_ASSIGNED and incident.owner:
        _enqueue(
            session,
            incident,
            provider,
            "assign_work_item",
            {"assignee": incident.owner},
        )
    neutral = _WORK_STATUS.get(to_status)
    if neutral is not None:
        _enqueue(
            session,
            incident,
            provider,
            "transition_work_item",
            {"status": neutral, "cause": cause},
        )
    if to_status in (IncidentStatus.REVIEW_PENDING, IncidentStatus.REJECTED):
        _enqueue(
            session,
            incident,
            provider,
            "comment_work_item",
            {"body": f"{incident.incident_id} -> {to_status.value}: {cause}"},
        )
    if to_status in (IncidentStatus.RECOVERED, IncidentStatus.ESCALATED, IncidentStatus.FAILED):
        _enqueue(
            session,
            incident,
            provider,
            "comment_work_item",
            {"body": f"{incident.incident_id} reached {to_status.value}: {cause}"},
        )


def enqueue_approval_request(
    session: Session, incident: models.Incident, approval: models.Approval
) -> None:
    """The 2.2 Slack surface for the 1.8 approval gate: when a pending
    approvals row is created, the interactive request goes out through
    the same outbox - the row is the truth, the message is a door to it."""
    _enqueue(
        session,
        incident,
        "slack",
        "request_approval",
        {
            "approval_id": approval.id,
            "action": approval.action,
            "text": (
                f"{incident.incident_id}: approval required for "
                f"{approval.action} (approval #{approval.id})"
            ),
        },
    )


def enqueue_review_request(
    session: Session, incident: models.Incident, review: models.ReviewRequest
) -> None:
    """2.6's "review request delivered through Slack with the reasoning
    attached" - the reviewer's name and WHY them, not just a ping. The
    dedicated op name carries the row id so the drain renders the
    interactive Approve/Request-changes buttons."""
    _enqueue(
        session,
        incident,
        "slack",
        "review_request",
        {
            "review_id": review.id,
            "text": (
                f"{incident.incident_id}: review requested from "
                f"{review.reviewer} - {review.rationale}"
            ),
        },
    )
