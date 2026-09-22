"""Cross-system consistency reconciliation (subphase 2.9).

The outbox guarantees what Helix MEANT to tell each provider; it cannot
guarantee the provider still holds it - a Jira issue deleted by hand, a
Notion page archived, a status changed outside Helix. This sweep is the
drift check: it compares each `work_items`/`knowledge_pages` mirror row
against the provider's own answer and enqueues the repair op through the
same outbox, so reconciliation rides the same durable path as everything
else rather than being a second write channel.

Run it from `helix sync` after the drain. It is read-mostly by design:
the only local writes are outbox enqueues.
"""

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from state import models
from state.models import OutboxStatus

if TYPE_CHECKING:
    from integrations.notion.client import NotionClient
    from integrations.work.contract import WorkManagementProvider


def _has_pending(session: Session, incident_pk: int, integration: str, op: str) -> bool:
    """True when an identical repair is already queued - a reconcile run
    must never stack duplicate repair ops on an undelivered queue."""
    return (
        session.scalar(
            select(models.IntegrationOutbox.id)
            .where(models.IntegrationOutbox.incident_id == incident_pk)
            .where(models.IntegrationOutbox.integration == integration)
            .where(models.IntegrationOutbox.op == op)
            .where(models.IntegrationOutbox.status == OutboxStatus.PENDING)
            .limit(1)
        )
        is not None
    )


def reconcile_work_items(
    session: Session, work: "WorkManagementProvider", *, provider: str | None = None
) -> dict[str, int]:
    """Check every mirrored work item against the provider; enqueue the
    repair for each drift found. Returns counts by finding kind."""
    name = provider or work.name
    found = {"missing_remote": 0, "status_drift": 0, "missing_terminal": 0}
    rows = list(
        session.scalars(select(models.WorkItem).where(models.WorkItem.provider == name)).all()
    )
    for item in rows:
        incident = item.incident
        try:
            remote = work.get_work_item(item.external_id)
        except Exception:
            continue  # provider unreachable - the drain's own retry path reports it
        if remote is None:
            # Deleted out from under Helix: recreate via the normal create
            # path (which re-mirrors) rather than orphaning the mirror.
            session.delete(item)
            if not _has_pending(session, incident.id, name, "create_work_item"):
                from integrations.lifecycle_sync import _work_spec

                session.add(
                    models.IntegrationOutbox(
                        incident_id=incident.id,
                        integration=name,
                        op="create_work_item",
                        payload=_work_spec(incident),
                    )
                )
            found["missing_remote"] += 1
            continue
        if remote.status != item.status:
            item.status = remote.status  # the provider's word refreshes the mirror
            found["status_drift"] += 1
        # A terminal incident whose item is still open is drift the other
        # direction: enqueue the closing transition it should have had.
        from state.machine import IncidentStatus

        if (
            incident.status == IncidentStatus.RECOVERED
            and remote.status not in ("done", "closed")
            and not _has_pending(session, incident.id, name, "transition_work_item")
        ):
            session.add(
                models.IntegrationOutbox(
                    incident_id=incident.id,
                    integration=name,
                    op="transition_work_item",
                    payload={"status": "done", "cause": "reconcile: incident recovered"},
                )
            )
            found["missing_terminal"] += 1
    session.flush()
    return found


def reconcile_knowledge_pages(session: Session, notion: "NotionClient") -> dict[str, int]:
    """A mirrored page missing in Notion gets a re-sync enqueued (the
    drain recreates it); the mirror row stays until the new page lands."""
    found = {"missing_remote": 0}
    for page in session.scalars(select(models.KnowledgePage)).all():
        try:
            remote = notion.get_page(page.external_id)
        except Exception:
            continue
        if remote is None and not _has_pending(session, page.incident_id, "notion", "sync_page"):
            session.delete(page)
            session.add(
                models.IntegrationOutbox(
                    incident_id=page.incident_id,
                    integration="notion",
                    op="sync_page",
                    payload={"recreated": "reconcile"},
                )
            )
            found["missing_remote"] += 1
    session.flush()
    return found
