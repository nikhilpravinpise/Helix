"""The integration outbox drain (subphases 2.2/2.3/2.4/2.9).

`lifecycle_sync.enqueue_for_transition` commits IntegrationOutbox rows in
the same transaction as the state change that motivated them; THIS
module is the other half - the thing that actually talks to Slack, the
work provider, and Notion.

Draining is idempotent and crash-safe by construction:

- a row only reaches `sent` after its provider call returned, so a crash
  mid-send leaves it `pending` and the next drain retries it;
- `create_work_item` consults both the local `work_items` mirror and the
  provider's own `find_by_incident` before creating, so a retried or
  re-enqueued create never produces a duplicate (2.3's exit criterion);
- a dependent op whose `work_items` row is missing first tries to
  re-derive it from the provider - a work item deleted out from under
  Helix is recreated (2.9's drift reconciliation), while one whose
  `create` permanently failed marks its dependents `failed` with the
  reason rather than dangling `pending` forever;
- `attempts`/`last_error` are the retry ledger; past
  `MAX_OUTBOX_ATTEMPTS` a row goes `failed` - visible, queryable, and
  never silently dropped.

Provider failures degrade the DELIVERY, never the canonical state: the
incident row and its transitions are already committed truth; the outbox
row is the promise the drain keeps retrying.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.logging import get_logger
from integrations.work.contract import WorkItemRejected
from state import models
from state.connections import active_connection
from state.machine import IncidentStatus
from state.models import OutboxStatus

if TYPE_CHECKING:
    from integrations.notion.client import NotionClient
    from integrations.slack.client import SlackClient
    from integrations.work.contract import WorkItemRef, WorkManagementProvider

#: A row that has failed this many times stops retrying and lands in
#: `failed` - loud in queries, silent nowhere.
MAX_OUTBOX_ATTEMPTS = 10

#: `integration` values that are work-management providers (2.10 adds
#: providers without a schema change - the column is a plain string, so
#: this set is the dispatch table, not a DB enum).
_WORK_INTEGRATIONS = frozenset({"jira", "linear", "trello", "memory"})


class PermanentDeliveryError(Exception):
    """The op can never succeed - a permanently failed create leaves its
    dependents undeliverable. Marks the row `failed` immediately rather
    than burning the retry budget on a known-dead end."""


class DependencyNotReady(Exception):
    """The op's precondition exists but is not delivered yet (e.g. its
    create_work_item is still pending). The row stays `pending` WITHOUT
    consuming an attempt - waiting is not failing."""


def _resolve_slack() -> "SlackClient | None":
    from integrations.slack import get_slack_client

    return get_slack_client()


def _resolve_slack_for(session: Session) -> "SlackClient | None":
    """The connected workspace's bot token wins; otherwise the .env one."""
    connection = active_connection(session, "slack")
    if connection is not None:
        from integrations.slack import WebSlackClient

        return WebSlackClient(connection.access_token)
    return _resolve_slack()


def _resolve_work() -> "WorkManagementProvider | None":
    from integrations.work import get_work_provider

    return get_work_provider()


def _resolve_work_for(kind: str) -> "WorkManagementProvider | None":
    if kind == get_settings().work_provider:
        return _resolve_work()
    from integrations.work import get_work_provider_for

    return get_work_provider_for(kind)


def resolve_work_for_session(session: Session, kind: str) -> "WorkManagementProvider | None":
    """A connected Jira account wins for kind "jira"; otherwise the .env path."""
    if kind == "jira":
        from integrations.work import connected_jira_provider

        provider = connected_jira_provider(session)
        if provider is not None:
            return provider
    return _resolve_work_for(kind)


def _resolve_notion() -> "NotionClient | None":
    from integrations.notion import get_notion_client

    return get_notion_client()


def slack_thread_for(session: Session, incident_pk: int) -> tuple[str, str | None] | None:
    """The Slack thread this incident's updates live in: the `ts` (and the
    channel id Slack answered with, absent on rows sent before it was kept)
    of the first successfully sent Slack row. Threading is recovered from the
    outbox itself, so a retried notify never starts a second thread."""
    row = session.scalar(
        select(models.IntegrationOutbox)
        .where(models.IntegrationOutbox.incident_id == incident_pk)
        .where(models.IntegrationOutbox.integration == "slack")
        .where(models.IntegrationOutbox.status == OutboxStatus.SENT)
        .order_by(models.IntegrationOutbox.id.asc())
        .limit(1)
    )
    if row is None:
        return None
    payload = row.payload or {}
    ts = payload.get("ts")
    if not ts:
        return None
    channel = payload.get("channel")
    return str(ts), str(channel) if channel else None


def _thread_ts_for(session: Session, incident_pk: int) -> str | None:
    """The `ts` of the thread this incident's Slack updates reply under."""
    thread = slack_thread_for(session, incident_pk)
    return thread[0] if thread else None


def _fail_or_retry(row: models.IntegrationOutbox, error: str) -> str:
    """The retry ledger: bounded attempts, then a visible terminal
    `failed`. Returns the outcome bucket for the drain summary."""
    row.attempts += 1
    row.last_error = error
    if row.attempts >= MAX_OUTBOX_ATTEMPTS:
        row.status = OutboxStatus.FAILED
        return "failed"
    return "pending"


def _work_item_for(session: Session, incident_pk: int, provider: str) -> models.WorkItem | None:
    return session.scalar(
        select(models.WorkItem)
        .where(models.WorkItem.incident_id == incident_pk)
        .where(models.WorkItem.provider == provider)
    )


def _persist_work_item(
    session: Session,
    incident_pk: int,
    provider: str,
    ref: "WorkItemRef",
    *,
    fallback_title: str = "",
    fallback_project: str | None = None,
) -> models.WorkItem:
    """Mirror the provider's item into `work_items` - or refresh the
    mirror row when the provider handed back an item we already track.
    `title` is NOT NULL locally but optional on the ref (a provider
    answering a get/find may not echo it), so the spec's title is the
    fallback on first persist."""
    row = _work_item_for(session, incident_pk, provider)
    if row is None:
        row = models.WorkItem(
            incident_id=incident_pk,
            provider=provider,
            external_id=ref.external_id,
            title=ref.title or fallback_title,
        )
        session.add(row)
    row.external_id = ref.external_id
    row.external_key = ref.external_key
    row.external_url = ref.external_url
    row.status = ref.status
    row.assignee = ref.assignee
    row.priority = ref.priority
    if ref.project:
        row.project = ref.project
    elif fallback_project and row.project is None:
        row.project = fallback_project
    session.flush()
    return row


def _send_slack(
    session: Session,
    row: models.IntegrationOutbox,
    incident: models.Incident,
    slack: "SlackClient",
) -> None:
    from agent.ops.registry import load_registry
    from integrations.slack.format import (
        approval_card,
        describe_incident,
        escape,
        incident_card,
        mention,
        review_card,
        update_text,
    )

    settings = get_settings()
    # An explicit payload channel wins, then the connected workspace's chosen
    # channel, then the configured default.
    connection = active_connection(session, "slack")
    stored_channel = connection.settings.get("channel") if connection else None
    channel = row.payload.get("channel") or stored_channel or settings.slack_default_channel
    text = escape(row.payload.get("text") or f"{incident.incident_id} update")
    registry = load_registry()
    people = registry.people
    view = describe_incident(
        incident,
        repo=settings.github_repo,
        ui_base=f"http://{settings.api_bind}:{settings.api_port}",
        people=people,
    )
    if not view.owner:
        # A recovery incident has no owner until someone is assigned; the
        # service's default owner is who an escalation should reach.
        entry = registry.get(incident.service)
        view.owner = entry.default_owner if entry else None

    if row.op == "request_approval":
        reason = next(
            (
                t.cause
                for t in reversed(incident.transitions)
                if t.to_status == IncidentStatus.AWAITING_APPROVAL
            ),
            "",
        )
        action = str(row.payload.get("action") or "an action")
        blocks = approval_card(
            view,
            approval_id=int(row.payload["approval_id"]),
            action=action,
            reason=reason,
            people=people,
        )
        fallback = (
            f"{mention(view.owner, people)} approval needed: {escape(action)} "
            f"for {view.incident_id}"
        )
        resp = slack.post_message(channel, fallback, blocks=blocks)
    elif row.op == "review_request":
        # 2.6: the reviewer's ask is interactive too - the buttons record
        # the decision on the durable review_requests row.
        review = session.get(models.ReviewRequest, int(row.payload["review_id"]))
        reviewer = review.reviewer if review else ""
        blocks = review_card(
            view,
            review_id=int(row.payload["review_id"]),
            reviewer=reviewer,
            rationale=review.rationale if review else "",
            people=people,
        )
        fallback = f"{mention(reviewer, people)} review requested: {view.summary}"
        resp = slack.post_message(channel, fallback, blocks=blocks)
    else:
        thread = _thread_ts_for(session, incident.id)
        if thread is None:
            # The first message is the incident card every update threads
            # under: what broke, who owns it, where to look.
            resp = slack.post_message(channel, view.summary, blocks=incident_card(view, people))
        else:
            status = row.payload.get("status")
            line = (
                update_text(
                    str(status),
                    str(row.payload.get("cause") or ""),
                    owner=view.owner,
                    people=people,
                )
                if status
                else text
            )
            resp = slack.post_message(channel, line, thread_ts=thread)
    if resp.get("ts"):
        # The channel id Slack answers with is what a thread link needs
        # (a configured channel may be a name); older rows lack it.
        recorded = {"ts": resp["ts"]}
        if resp.get("channel"):
            recorded["channel"] = resp["channel"]
        row.payload = {**row.payload, **recorded}


def _send_work(
    session: Session,
    row: models.IntegrationOutbox,
    incident: models.Incident,
    work: "WorkManagementProvider",
) -> None:
    from integrations.work.contract import WorkItemSpec

    provider = row.integration
    op = row.op
    if op == "create_work_item":
        # Duplicate prevention (2.3): the local mirror first, then the
        # provider's own dedup lookup - only then a real create.
        existing = _work_item_for(session, incident.id, provider)
        if existing is not None:
            row.payload = {**row.payload, "deduplicated": "local"}
            return
        found = work.find_by_incident(incident.incident_id)
        if found is not None:
            _persist_work_item(
                session,
                incident.id,
                provider,
                found,
                fallback_title=str(row.payload.get("title") or incident.incident_id),
                fallback_project=row.payload.get("project"),
            )
            row.payload = {**row.payload, "deduplicated": "provider"}
            return
        spec = WorkItemSpec.model_validate({k: v for k, v in row.payload.items() if k != "cause"})
        ref = work.create_work_item(spec)
        _persist_work_item(
            session,
            incident.id,
            provider,
            ref,
            fallback_title=spec.title,
            fallback_project=spec.project,
        )
        return

    item = _work_item_for(session, incident.id, provider)
    if item is None:
        # The mirror is empty - reconcile before concluding anything
        # (2.9): the provider may still hold the item (a lost mirror), or
        # may have lost it too (external deletion - recreate below).
        found = work.find_by_incident(incident.incident_id)
        if found is not None:
            item = _persist_work_item(
                session,
                incident.id,
                provider,
                found,
                fallback_title=incident.incident_id,
            )
        else:
            create_row = session.scalar(
                select(models.IntegrationOutbox)
                .where(models.IntegrationOutbox.incident_id == incident.id)
                .where(models.IntegrationOutbox.integration == provider)
                .where(models.IntegrationOutbox.op == "create_work_item")
                .order_by(models.IntegrationOutbox.id.desc())
                .limit(1)
            )
            if create_row is not None and create_row.status == OutboxStatus.FAILED:
                raise PermanentDeliveryError(
                    "create_work_item permanently failed for this incident"
                )
            if create_row is not None and create_row.status == OutboxStatus.PENDING:
                raise DependencyNotReady("create_work_item is still pending")
            if op in ("assign_work_item", "transition_work_item", "comment_work_item"):
                # The item is gone on both sides - recreate it so the
                # lifecycle op has something to land on (external drift).
                spec = WorkItemSpec.model_validate(_spec_payload(incident))
                item = _persist_work_item(
                    session,
                    incident.id,
                    provider,
                    work.create_work_item(spec),
                    fallback_title=spec.title,
                    fallback_project=spec.project,
                )
            else:
                raise PermanentDeliveryError(f"no work item for {op}")
    else:
        # The mirror says the item exists - verify the remote still does
        # before a dependent op lands on it (2.9 drift: an item deleted
        # out-of-band, or the memory provider in a fresh process). A
        # provider error here propagates to the retry path rather than
        # guessing - unreachable is not deleted.
        if work.get_work_item(item.external_id) is None:
            spec = WorkItemSpec.model_validate(_spec_payload(incident))
            item = _persist_work_item(
                session,
                incident.id,
                provider,
                work.create_work_item(spec),
                fallback_title=spec.title,
                fallback_project=spec.project,
            )

    if op == "update_work_item":
        ref = work.update_work_item(
            item.external_id,
            title=row.payload.get("title"),
            description=row.payload.get("description"),
            priority=row.payload.get("priority"),
        )
    elif op == "assign_work_item":
        ref = work.assign_work_item(item.external_id, row.payload.get("assignee"))
    elif op == "transition_work_item":
        ref = work.transition_work_item(item.external_id, str(row.payload["status"]))
    elif op == "comment_work_item":
        ref = None
        work.comment_work_item(item.external_id, str(row.payload.get("body") or ""))
        session.add(
            models.WorkItemComment(work_item_id=item.id, body=str(row.payload.get("body") or ""))
        )
    elif op == "link_work_item":
        ref = None
        work.link_work_item(
            item.external_id,
            str(row.payload["other_external_id"]),
            link_type=str(row.payload.get("link_type") or "relates"),
        )
    else:
        raise PermanentDeliveryError(f"unknown work op {op!r}")
    if ref is not None:
        _persist_work_item(session, incident.id, provider, ref, fallback_title=incident.incident_id)


def _spec_payload(incident: models.Incident) -> dict[str, Any]:
    """Re-derive the create spec for drift-recovery recreates - the same
    shape lifecycle_sync._work_spec enqueues."""
    from integrations.lifecycle_sync import _work_spec

    return _work_spec(incident)


def _send_notion(
    session: Session,
    row: models.IntegrationOutbox,
    incident: models.Incident,
    notion: "NotionClient",
) -> None:
    from integrations.notion.pages import build_incident_page

    settings = get_settings()
    title, blocks = build_incident_page(session, incident)
    page = session.scalar(
        select(models.KnowledgePage).where(models.KnowledgePage.incident_id == incident.id)
    )
    if page is not None:
        # Drift check (2.9): a page deleted in Notion is recreated rather
        # than appended-to-blindly; the mirror row follows the new id.
        remote = notion.get_page(page.external_id)
        if remote is None:
            resp = notion.create_page(settings.notion_parent_page_id, title, blocks)
            page.external_id = str(resp["id"])
            page.external_url = resp.get("url")
            page.title = title
            session.flush()
        else:
            notion.append_blocks(page.external_id, blocks)
            page.title = title
        return
    resp = notion.create_page(settings.notion_parent_page_id, title, blocks)
    session.add(
        models.KnowledgePage(
            incident_id=incident.id,
            provider="notion",
            external_id=str(resp["id"]),
            external_url=resp.get("url"),
            title=title,
        )
    )
    session.flush()


def _claim_next(session: Session, after_id: int) -> models.IntegrationOutbox | None:
    """The oldest pending row after `after_id`, locked for this drain.

    SKIP LOCKED: a row another drain is delivering is passed over rather
    than waited on or sent a second time - a Slack click's resume and a
    `helix sync` can drain at once. SQLite ignores the lock clause."""
    return session.scalar(
        select(models.IntegrationOutbox)
        .where(models.IntegrationOutbox.status == OutboxStatus.PENDING)
        .where(models.IntegrationOutbox.id > after_id)
        .order_by(models.IntegrationOutbox.id.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def drain_outbox(
    session: Session,
    *,
    slack: "SlackClient | None" = None,
    work: "WorkManagementProvider | None" = None,
    notion: "NotionClient | None" = None,
    limit: int = 200,
) -> dict[str, int]:
    """Deliver pending outbox rows, oldest first. Returns outcome counts.

    Clients are injectable for tests; when not passed they resolve from
    settings, and an integration with no credentials records each of its
    rows `skipped` (delivered-to-nowhere is an explicit outcome, never a
    silent backlog or a fake `sent`).

    Commits after every row. Delivery is an outside effect that cannot be
    rolled back, so its `sent` mark must not be either: rows used to stay
    uncommitted until the caller's transaction ended, and an error after
    the drain (helix sync reconciles in the same session) rolled every
    delivered row back to pending, so the next drain posted it all again.
    A crash between a delivery and its commit can still repeat that one
    row - at least once, never a batch. The commit also ends the caller's
    open transaction, so callers drain after their own state is final.
    """
    summary = {"sent": 0, "skipped": 0, "failed": 0, "pending": 0}
    log = get_logger()
    # Resolve each client once per distinct integration for the whole
    # pass. A provider whose state is per-instance - the memory provider
    # is exactly that - must see the item an earlier row in THIS drain
    # created; resolving per row gave every op a fresh, empty provider,
    # so a comment op after a transition would miss the remote, recreate
    # it, and clobber the mirror's status back to `open`.
    slack_client = slack if slack is not None else _resolve_slack_for(session)
    notion_client = notion if notion is not None else _resolve_notion()
    work_providers: dict[str, WorkManagementProvider] = {}
    injected_name = getattr(work, "name", "") if work is not None else ""

    def _work_for(kind: str) -> "WorkManagementProvider | None":
        provider = work_providers.get(kind)
        if provider is None:
            if work is not None and (not injected_name or kind == injected_name):
                provider = work
            else:
                provider = resolve_work_for_session(session, kind)
            if provider is not None:
                work_providers[kind] = provider
        return provider

    last_id = 0
    for _ in range(max(1, limit)):
        row = _claim_next(session, last_id)
        if row is None:
            break
        last_id = row.id
        _deliver_one(session, row, summary, log, slack_client, notion_client, _work_for)
        session.commit()
    return summary


def _deliver_one(
    session: Session,
    row: models.IntegrationOutbox,
    summary: dict[str, int],
    log: Any,
    slack_client: "SlackClient | None",
    notion_client: "NotionClient | None",
    work_for: Callable[[str], "WorkManagementProvider | None"],
) -> None:
    """Deliver one claimed row and record its outcome on it."""
    incident = row.incident
    try:
        if row.integration == "slack":
            if slack_client is None:
                row.status = OutboxStatus.SKIPPED
                row.last_error = "slack not configured"
                summary["skipped"] += 1
                return
            _send_slack(session, row, incident, slack_client)
        elif row.integration == "notion":
            if notion_client is None:
                row.status = OutboxStatus.SKIPPED
                row.last_error = "notion not configured"
                summary["skipped"] += 1
                return
            _send_notion(session, row, incident, notion_client)
        elif row.integration in _WORK_INTEGRATIONS:
            work_provider = work_for(row.integration)
            if work_provider is None:
                row.status = OutboxStatus.SKIPPED
                row.last_error = "work provider not configured"
                summary["skipped"] += 1
                return
            _send_work(session, row, incident, work_provider)
        else:
            row.status = OutboxStatus.FAILED
            row.last_error = f"unknown integration {row.integration!r}"
            summary["failed"] += 1
            return
    except DependencyNotReady:
        # Deferred, not attempted: the row waits for its dependency
        # without burning retry budget.
        summary["pending"] += 1
        return
    except (PermanentDeliveryError, WorkItemRejected) as exc:
        # WorkItemRejected: the provider refused (no such workflow
        # state, no such user). Retrying cannot change the answer.
        row.status = OutboxStatus.FAILED
        row.last_error = str(exc)
        summary["failed"] += 1
        return
    except Exception as exc:  # one bad row never stalls the queue
        outcome = _fail_or_retry(row, str(exc))
        summary[outcome] += 1
        log.warning(
            "outbox delivery failed",
            outbox_id=row.id,
            integration=row.integration,
            op=row.op,
            error=str(exc),
        )
        return
    if row.status == OutboxStatus.PENDING:
        row.status = OutboxStatus.SENT
    row.sent_at = datetime.now(UTC)
    summary["sent"] += 1
