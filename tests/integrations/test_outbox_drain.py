"""Outbox drain + lifecycle-sync tests (subphases 2.2/2.3/2.4/2.9).

The contract under test: a transition commits its external effects as
`integration_outbox` rows in the same transaction; `drain_outbox`
delivers them idempotently - never twice, never lost on outage, and
reconciling drift rather than pretending it cannot happen.
"""

import functools
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from integrations.lifecycle_sync import enqueue_for_transition
from integrations.outbox import MAX_OUTBOX_ATTEMPTS, drain_outbox
from state import models
from state.db import session_scope
from state.machine import IncidentStatus
from state.models import OutboxStatus


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


def _incident(session: Session, *, kind: str = "change", status=IncidentStatus.RECEIVED):
    incident = models.Incident(
        incident_id=f"INC-{uuid.uuid4().hex[:8]}",
        kind=models.IncidentKind(kind),
        service="payment-api",
        environment="sandbox",
        severity="high",
        status=status,
        fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
    )
    session.add(incident)
    session.flush()
    return incident


def _pending(session: Session) -> list[models.IntegrationOutbox]:
    return list(
        session.scalars(
            select(models.IntegrationOutbox)
            .where(models.IntegrationOutbox.status == OutboxStatus.PENDING)
            .order_by(models.IntegrationOutbox.id)
        ).all()
    )


class _RecordingSlack:
    def __init__(self):
        self.calls: list[dict] = []
        self._n = 0

    def post_message(self, channel, text, *, thread_ts=None, blocks=None):
        self._n += 1
        self.calls.append(
            {
                "op": "post",
                "channel": channel,
                "text": text,
                "thread_ts": thread_ts,
                "blocks": blocks,
            }
        )
        return {"ok": True, "ts": f"1000.{self._n}"}

    def update_message(self, channel, ts, text, *, blocks=None):
        self.calls.append({"op": "update", "channel": channel, "ts": ts, "text": text})
        return {"ok": True, "ts": ts}


class _RecordingWork:
    """A minimal in-memory WorkManagementProvider for drain tests - the
    real conformance suite lives in test_work_contract.py; this records
    just enough to prove the drain called the contract correctly."""

    name = "memory"

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self._n = 0
        self.fail_with: Exception | None = None

    def _maybe_fail(self):
        if self.fail_with is not None:
            raise self.fail_with

    def capabilities(self):
        from integrations.work.contract import ProviderCapabilities

        return ProviderCapabilities()

    def create_work_item(self, spec):
        self._maybe_fail()
        self._n += 1
        external_id = f"MEM-{self._n}"
        self.items[external_id] = {
            "incident_id": spec.incident_id,
            "status": "open",
            "assignee": spec.assignee,
            "title": spec.title,
        }
        self.calls.append(("create", spec.incident_id))
        from integrations.work.contract import WorkItemRef

        return WorkItemRef(external_id=external_id, external_key=external_id, status="open")

    def get_work_item(self, external_id):
        self._maybe_fail()
        item = self.items.get(external_id)
        if item is None:
            return None
        from integrations.work.contract import WorkItemRef

        return WorkItemRef(
            external_id=external_id,
            status=item["status"],
            assignee=item.get("assignee"),
        )

    def find_by_incident(self, incident_id):
        self._maybe_fail()
        for external_id, item in self.items.items():
            if item["incident_id"] == incident_id:
                from integrations.work.contract import WorkItemRef

                return WorkItemRef(
                    external_id=external_id,
                    status=item["status"],
                    assignee=item.get("assignee"),
                )
        return None

    def update_work_item(self, external_id, *, title=None, description=None, priority=None):
        self._maybe_fail()
        return self.get_work_item(external_id)

    def assign_work_item(self, external_id, assignee):
        self._maybe_fail()
        self.items[external_id]["assignee"] = assignee
        self.calls.append(("assign", external_id, assignee))
        return self.get_work_item(external_id)

    def transition_work_item(self, external_id, status):
        self._maybe_fail()
        self.items[external_id]["status"] = status
        self.calls.append(("transition", external_id, status))
        return self.get_work_item(external_id)

    def comment_work_item(self, external_id, body, *, author="helix"):
        self._maybe_fail()
        self.calls.append(("comment", external_id, body))

    def link_work_item(self, external_id, other_external_id, *, link_type="relates"):
        self._maybe_fail()
        self.calls.append(("link", external_id, other_external_id, link_type))


class _RecordingNotion:
    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self._n = 0
        self.fail_with: Exception | None = None

    def _maybe_fail(self):
        if self.fail_with is not None:
            raise self.fail_with

    def create_page(self, parent_id, title, blocks):
        self._maybe_fail()
        self._n += 1
        page_id = f"mem-page-{self._n}"
        self.pages[page_id] = {"title": title, "blocks": list(blocks), "parent": parent_id}
        self.calls.append(("create", page_id, title))
        return {"id": page_id, "url": f"https://notion.so/{page_id}"}

    def append_blocks(self, page_id, blocks):
        self._maybe_fail()
        self.pages[page_id]["blocks"].extend(blocks)
        self.calls.append(("append", page_id, len(blocks)))
        return {"id": page_id}

    def get_page(self, page_id):
        self._maybe_fail()
        return self.pages.get(page_id)


def test_transition_enqueues_slack_work_and_notion_ops(sessions):
    with sessions() as session:
        incident = _incident(session)
        enqueue_for_transition(session, incident, IncidentStatus.TRIAGED, cause="owner alice")
        ops = [(r.integration, r.op) for r in _pending(session)]
        assert ("slack", "notify") in ops
        # work provider defaults to memory; the create op is enqueued
        assert any(op == "create_work_item" for _, op in ops)
        # triaged is not a page-sync milestone; owner_assigned is - and it
        # only assigns when the incident actually carries an owner (the
        # coordinator sets owner before enqueueing this transition).
        incident.owner = "alice"
        enqueue_for_transition(
            session, incident, IncidentStatus.OWNER_ASSIGNED, cause="owner alice"
        )
        ops = [(r.integration, r.op) for r in _pending(session)]
        assert ("notion", "sync_page") in ops
        assert any(op == "assign_work_item" for _, op in ops)


def test_drain_delivers_notify_and_threads_followups(sessions):
    slack = _RecordingSlack()
    with sessions() as session:
        incident = _incident(session)
        enqueue_for_transition(session, incident, IncidentStatus.TRIAGED, cause="a")
        enqueue_for_transition(session, incident, IncidentStatus.OWNER_ASSIGNED, cause="b")
        summary = drain_outbox(
            session, slack=slack, work=_RecordingWork(), notion=_RecordingNotion()
        )
        assert summary["sent"] >= 2
        notify_calls = [c for c in slack.calls if c["op"] == "post"]
        # First notify starts the thread; the follow-up replies inside it.
        assert notify_calls[0]["thread_ts"] is None
        assert notify_calls[1]["thread_ts"] == "1000.1"


def test_drain_create_work_item_persists_mirror_and_dedups(sessions):
    work = _RecordingWork()
    with sessions() as session:
        incident = _incident(session)
        enqueue_for_transition(session, incident, IncidentStatus.TRIAGED)
        drain_outbox(session, slack=_RecordingSlack(), work=work, notion=_RecordingNotion())
        item = session.scalar(select(models.WorkItem))
        # The mirror points at the item the provider actually holds.
        assert item is not None and item.external_id in work.items
        # A second create op (re-enqueued or replayed) must not create a
        # second item - 2.3's duplicate-prevention exit criterion.
        enqueue_for_transition(session, incident, IncidentStatus.DIAGNOSING)
        drain_outbox(session, slack=_RecordingSlack(), work=work, notion=_RecordingNotion())
        creates = [c for c in work.calls if c[0] == "create"]
        assert len(creates) == 1


def test_drain_transition_and_comment_ops_hit_the_mirrored_item(sessions):
    work = _RecordingWork()
    with sessions() as session:
        incident = _incident(session)
        enqueue_for_transition(session, incident, IncidentStatus.TRIAGED)
        enqueue_for_transition(session, incident, IncidentStatus.REVIEW_PENDING, cause="bob")
        enqueue_for_transition(session, incident, IncidentStatus.RECOVERED, cause="done")
        drain_outbox(session, slack=_RecordingSlack(), work=work, notion=_RecordingNotion())
        mirrored = session.scalar(select(models.WorkItem)).external_id
        transitions = [c for c in work.calls if c[0] == "transition"]
        assert ("transition", mirrored, "in_progress") in transitions
        assert ("transition", mirrored, "done") in transitions
        comments = [c for c in work.calls if c[0] == "comment"]
        assert comments  # review_pending + recovered each comment
        # And the local comment audit row exists.
        assert session.scalar(select(models.WorkItemComment.id)) is not None


def test_provider_outage_leaves_rows_pending_then_recovers(sessions):
    work = _RecordingWork()
    work.fail_with = RuntimeError("jira down")
    with sessions() as session:
        incident = _incident(session)
        enqueue_for_transition(session, incident, IncidentStatus.TRIAGED)
        summary = drain_outbox(
            session, slack=_RecordingSlack(), work=work, notion=_RecordingNotion()
        )
        # The slack notify still delivered; the work op retried.
        assert summary["pending"] >= 1
        work.fail_with = None
        summary = drain_outbox(
            session, slack=_RecordingSlack(), work=work, notion=_RecordingNotion()
        )
        assert summary["pending"] == 0
        assert session.scalar(select(models.WorkItem)) is not None


def test_permanent_failure_marks_row_failed_after_budget(sessions):
    work = _RecordingWork()
    work.fail_with = RuntimeError("down")
    with sessions() as session:
        incident = _incident(session)
        enqueue_for_transition(session, incident, IncidentStatus.TRIAGED)
        for _ in range(MAX_OUTBOX_ATTEMPTS):
            drain_outbox(session, slack=_RecordingSlack(), work=work, notion=_RecordingNotion())
        create_row = session.scalar(
            select(models.IntegrationOutbox).where(
                models.IntegrationOutbox.op == "create_work_item"
            )
        )
        assert create_row.status == OutboxStatus.FAILED
        # A dependent op enqueued after the failure resolves to failed
        # once the provider is back and confirms nothing exists - while
        # the provider is down it defers (a pending dependency is not a
        # failure).
        enqueue_for_transition(session, incident, IncidentStatus.OWNER_ASSIGNED)
        drain_outbox(session, slack=_RecordingSlack(), work=work, notion=_RecordingNotion())
        transition_row = session.scalar(
            select(models.IntegrationOutbox).where(
                models.IntegrationOutbox.op == "transition_work_item"
            )
        )
        assert transition_row.status == OutboxStatus.PENDING
        work.fail_with = None
        drain_outbox(session, slack=_RecordingSlack(), work=work, notion=_RecordingNotion())
        transition_row = session.scalar(
            select(models.IntegrationOutbox).where(
                models.IntegrationOutbox.op == "transition_work_item"
            )
        )
        assert transition_row.status == OutboxStatus.FAILED


def test_drift_deleted_work_item_is_recreated(sessions):
    work = _RecordingWork()
    with sessions() as session:
        incident = _incident(session)
        enqueue_for_transition(session, incident, IncidentStatus.TRIAGED)
        drain_outbox(session, slack=_RecordingSlack(), work=work, notion=_RecordingNotion())
        item = session.scalar(select(models.WorkItem))
        original = item.external_id
        session.delete(item)  # mirror lost
        work.items.clear()  # remote lost too
        session.flush()
        enqueue_for_transition(session, incident, IncidentStatus.RECOVERED)
        drain_outbox(session, slack=_RecordingSlack(), work=work, notion=_RecordingNotion())
        # The recovered comment/transition ops had nowhere to land, so the
        # drain recreated the item first (2.9 drift reconciliation).
        item = session.scalar(select(models.WorkItem))
        assert item is not None and item.external_id != original


def test_notion_sync_creates_then_appends_and_recreates(sessions):
    notion = _RecordingNotion()
    with sessions() as session:
        incident = _incident(session)
        enqueue_for_transition(session, incident, IncidentStatus.OWNER_ASSIGNED)
        drain_outbox(session, slack=_RecordingSlack(), work=_RecordingWork(), notion=notion)
        page = session.scalar(select(models.KnowledgePage))
        assert page is not None and page.external_id == "mem-page-1"
        enqueue_for_transition(session, incident, IncidentStatus.APPROVED)
        drain_outbox(session, slack=_RecordingSlack(), work=_RecordingWork(), notion=notion)
        assert any(c[0] == "append" and c[1] == "mem-page-1" for c in notion.calls)
        # Drift: the page is deleted in Notion; the next sync recreates it.
        notion.pages.clear()
        enqueue_for_transition(session, incident, IncidentStatus.RECOVERED)
        drain_outbox(session, slack=_RecordingSlack(), work=_RecordingWork(), notion=notion)
        page = session.scalar(select(models.KnowledgePage))
        assert page.external_id != "mem-page-1"


def test_unconfigured_integrations_record_skipped(sessions, monkeypatch):
    monkeypatch.setattr("integrations.outbox._resolve_slack", lambda: None)
    monkeypatch.setattr("integrations.outbox._resolve_work", lambda: None)
    monkeypatch.setattr("integrations.outbox._resolve_notion", lambda: None)
    with sessions() as session:
        incident = _incident(session)
        enqueue_for_transition(session, incident, IncidentStatus.TRIAGED)
        # Passing nothing resolves through the (monkeypatched) factories.
        summary = drain_outbox(session)
        assert summary["skipped"] >= 1
        rows = session.scalars(select(models.IntegrationOutbox)).all()
        assert all(r.status in (OutboxStatus.SKIPPED, OutboxStatus.FAILED) for r in rows)


def test_approval_request_op_posts_blocks(sessions):
    slack = _RecordingSlack()
    with sessions() as session:
        incident = _incident(session)
        approval = models.Approval(
            incident_id=incident.id,
            action="rollout_restart",
            params={},
            fingerprint="fp1",
            status=models.ApprovalStatus.PENDING,
        )
        session.add(approval)
        session.flush()
        from integrations.lifecycle_sync import enqueue_approval_request

        enqueue_approval_request(session, incident, approval)
        drain_outbox(session, slack=slack, work=_RecordingWork(), notion=_RecordingNotion())
        call = slack.calls[0]
        assert call["blocks"] is not None
        action_ids = [
            a["action_id"]
            for b in call["blocks"]
            for a in (b.get("elements") or [])
            if a.get("action_id")
        ]
        assert f"helix_approve:{approval.id}" in action_ids
        assert f"helix_reject:{approval.id}" in action_ids


def test_recovery_incident_gets_work_item_at_diagnosing(sessions):
    work = _RecordingWork()
    with sessions() as session:
        incident = _incident(session, kind="recovery")
        enqueue_for_transition(session, incident, IncidentStatus.DIAGNOSING)
        drain_outbox(session, slack=_RecordingSlack(), work=work, notion=_RecordingNotion())
        item = session.scalar(select(models.WorkItem))
        assert item is not None


def test_unknown_integration_marks_failed(sessions):
    with sessions() as session:
        incident = _incident(session)
        session.add(
            models.IntegrationOutbox(
                incident_id=incident.id,
                integration="pagerduty",
                op="notify",
                payload={},
            )
        )
        session.flush()
        summary = drain_outbox(
            session, slack=_RecordingSlack(), work=_RecordingWork(), notion=_RecordingNotion()
        )
        assert summary["failed"] == 1


def test_a_provider_refusal_fails_at_once_instead_of_retrying(sessions):
    """Live Jira, 2026-10-03: an escalated incident's ticket could not move
    to "blocked" because the Kanban board has no such column. The provider
    raised a plain WorkItemError, so the drain treated a refusal no retry
    can change like a network blip and kept the row pending, spending its
    retry budget while the ticket sat in "In Progress". A WorkItemRejected
    is the provider saying no, and fails the row on the first attempt."""
    from integrations.work.contract import WorkItemRejected

    work = _RecordingWork()
    with sessions() as session:
        incident = _incident(session)
        enqueue_for_transition(session, incident, IncidentStatus.TRIAGED)
        drain_outbox(session, slack=_RecordingSlack(), work=work, notion=_RecordingNotion())
        work.fail_with = WorkItemRejected("jira workflow offers no transition for 'blocked'")
        enqueue_for_transition(session, incident, IncidentStatus.ESCALATED, cause="gave up")
        drain_outbox(session, slack=_RecordingSlack(), work=work, notion=_RecordingNotion())
        refused = session.scalars(
            select(models.IntegrationOutbox).where(
                models.IntegrationOutbox.op == "transition_work_item"
            )
        ).all()[-1]
        assert refused.status == OutboxStatus.FAILED
        assert "no transition" in refused.last_error
