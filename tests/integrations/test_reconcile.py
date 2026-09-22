"""Drift-reconciliation sweep tests (subphase 2.9).

`helix sync` runs the drain, then `reconcile_*` compares every mirror row
against the provider's own answer and enqueues repair ops - so a work
item deleted by hand in Jira, or a Notion page archived out of band, is
detected and re-created through the same durable outbox path.
"""

import functools
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from integrations.notion import MemoryNotionClient
from integrations.outbox import drain_outbox
from integrations.reconcile import reconcile_knowledge_pages, reconcile_work_items
from integrations.work import MemoryWorkProvider
from integrations.work.contract import WorkItemSpec
from state import models
from state.db import session_scope
from state.machine import IncidentStatus


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def sessions(engine):
    return functools.partial(session_scope, engine)


def _incident(session: Session, *, status=IncidentStatus.RECEIVED) -> models.Incident:
    incident = models.Incident(
        incident_id=f"INC-{uuid.uuid4().hex[:8]}",
        kind=models.IncidentKind.CHANGE,
        service="payment-api",
        environment="sandbox",
        severity="high",
        status=status,
        fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
    )
    session.add(incident)
    session.flush()
    return incident


def _mirror_item(
    session: Session,
    incident: models.Incident,
    work: MemoryWorkProvider,
    *,
    status: str = "open",
) -> models.WorkItem:
    ref = work.create_work_item(
        WorkItemSpec(
            incident_id=incident.incident_id,
            title=f"Helix: {incident.incident_id} - payment-api",
            description="test",
            service="payment-api",
            environment="sandbox",
            severity="high",
        )
    )
    ref.status = status
    item = models.WorkItem(
        incident_id=incident.id,
        provider="memory",
        external_id=ref.external_id,
        title=ref.title or "t",
        status=status,
    )
    work.items[ref.external_id]["status"] = status
    session.add(item)
    session.flush()
    return item


def test_remote_deleted_work_item_enqueues_recreate(sessions):
    work = MemoryWorkProvider()
    with sessions() as session:
        incident = _incident(session)
        _mirror_item(session, incident, work)
        work.items.clear()  # deleted out of band
        found = reconcile_work_items(session, work)
        assert found["missing_remote"] == 1
        # The mirror row is dropped and a fresh create op queued.
        assert session.scalar(select(models.WorkItem)) is None
        ops = [
            (r.integration, r.op) for r in session.scalars(select(models.IntegrationOutbox)).all()
        ]
        assert ("memory", "create_work_item") in ops


def test_status_drift_refreshes_mirror(sessions):
    work = MemoryWorkProvider()
    with sessions() as session:
        incident = _incident(session)
        item = _mirror_item(session, incident, work)
        work.items[item.external_id]["status"] = "in_progress"  # moved in the provider
        found = reconcile_work_items(session, work)
        assert found["status_drift"] == 1
        assert session.get(models.WorkItem, item.id).status == "in_progress"


def test_recovered_incident_with_open_item_enqueues_closing_transition(sessions):
    work = MemoryWorkProvider()
    with sessions() as session:
        incident = _incident(session, status=IncidentStatus.RECOVERED)
        _mirror_item(session, incident, work, status="in_progress")
        work.items[list(work.items)[0]]["status"] = "in_progress"
        found = reconcile_work_items(session, work)
        assert found["missing_terminal"] == 1
        transition_op = session.scalar(
            select(models.IntegrationOutbox).where(
                models.IntegrationOutbox.op == "transition_work_item"
            )
        )
        assert transition_op is not None
        assert transition_op.payload["status"] == "done"


def test_reconcile_is_idempotent_while_repairs_pending(sessions):
    work = MemoryWorkProvider()
    with sessions() as session:
        incident = _incident(session, status=IncidentStatus.RECOVERED)
        _mirror_item(session, incident, work, status="in_progress")
        work.items[list(work.items)[0]]["status"] = "in_progress"
        reconcile_work_items(session, work)
        found = reconcile_work_items(session, work)  # second pass
        # The pending repair already covers the drift - no stacking.
        assert found["missing_terminal"] == 0
        assert (
            session.scalars(
                select(models.IntegrationOutbox).where(
                    models.IntegrationOutbox.op == "transition_work_item"
                )
            ).all()
            != []
        )


def test_provider_outage_is_skipped_not_destructive(sessions):
    work = MemoryWorkProvider()
    with sessions() as session:
        incident = _incident(session)
        item = _mirror_item(session, incident, work)
        work.fail_with = RuntimeError("jira unreachable")
        found = reconcile_work_items(session, work)
        assert found == {"missing_remote": 0, "status_drift": 0, "missing_terminal": 0}
        # An outage must NOT delete the mirror - unreachable is not gone.
        assert session.get(models.WorkItem, item.id) is not None


def test_notion_page_deleted_enqueues_resync(sessions):
    notion = MemoryNotionClient()
    with sessions() as session:
        incident = _incident(session)
        resp = notion.create_page("root", "title", [])
        session.add(
            models.KnowledgePage(
                incident_id=incident.id,
                provider="notion",
                external_id=resp["id"],
                external_url=resp.get("url"),
                title="title",
            )
        )
        session.flush()
        notion.pages.clear()  # archived out of band
        found = reconcile_knowledge_pages(session, notion)
        assert found["missing_remote"] == 1
        assert session.scalar(select(models.KnowledgePage)) is None
        assert (
            session.scalar(
                select(models.IntegrationOutbox).where(
                    models.IntegrationOutbox.integration == "notion"
                )
            ).op
            == "sync_page"
        )


def test_reconcile_then_drain_repairs_end_to_end(sessions):
    """The full 2.9 loop: drift detected -> repair enqueued -> drained
    into a fresh remote item, mirror repointed."""
    work = MemoryWorkProvider()
    notion = MemoryNotionClient()
    with sessions() as session:
        incident = _incident(session)
        item = _mirror_item(session, incident, work)
        old_external = item.external_id
        work.items.clear()
        reconcile_work_items(session, work)
        drain_outbox(session, slack=None, work=work, notion=notion)
        item = session.scalar(select(models.WorkItem))
        assert item is not None and item.external_id != old_external
        assert len(work.items) == 1
