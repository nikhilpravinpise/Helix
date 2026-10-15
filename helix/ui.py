"""Operator UI (subphase 1.12, D11): server-rendered pages on the same
FastAPI app as the JSON API - no node toolchain, no build step.

Pages (the JSON API keeps its own routes, unchanged):

    GET /                      landing page when signed out, else the dashboard
    GET /ui/                   Home: what needs you, open incidents, 30 day
                               stats, recent activity, connected tools
    GET /ui/incidents          incident list (status, severity, age)
    GET /ui/incidents/{id}     incident header + full provenance timeline
                               (trigger -> evidence -> hypothesis ->
                               candidates -> decision -> validation ->
                               approval -> execution -> verification),
                               assembled by state.provenance; unknown id
                               renders a 404 page
    GET  /ui/connections       connect Slack and other accounts
                               (helix/connections_ui.py)
    POST /ui/samples           recorded evidence only: send a test incident
                               incident, drive it live in the background and
                               redirect to its page (one at a time; a start
                               during a run redirects to the running one)
    GET /ui/approvals          pending approval queue + decided rows;
                               approve/reject buttons POST to the 1.8 JSON
                               endpoints (POST /approvals/{id}/approve|reject)

Templates and static assets resolve from the repo-root `ui/` directory
(dev-mode resolution - fine for Phase 1; a packaged install would need
package-data wiring). `ui/static/app.js` is minimal vanilla JS: the
incident detail page polls `GET /incidents/{id}` while the incident is
non-terminal (status reads the DB, so it reflects any driving process),
and the approvals page calls the decision endpoints via fetch(). Auth is
unchanged: the single-user local surface shares the intake-token gate -
when HELIX_INTAKE_TOKEN is set the approvals page exposes a token field
and the JS sends it as a Bearer header.
"""

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent.ops.registry import load_registry
from helix import demo
from helix.config import get_settings
from state import connections, models, pr_bot
from state.db import session_scope
from state.deciders import approval_deciders, approval_refusal, review_refusal
from state.machine import TERMINAL_STATUSES, IncidentStatus
from state.provenance import assemble_provenance

# Repo-root ui/ directory - sibling of the helix package.
UI_DIR = Path(__file__).resolve().parents[1] / "ui"

templates = Jinja2Templates(directory=str(UI_DIR / "templates"))


def _fmt_dt(value: Any) -> str:
    """ISO string or datetime -> 'YYYY-MM-DD HH:MM:SSZ' (naive read as UTC)."""
    if value is None:
        return "-"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _age(value: Any) -> str:
    """Relative age of an ISO string / datetime: '42s', '5m', '3h', '2d'."""
    if value is None:
        return "-"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - value.astimezone(UTC)).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _jsonpp(value: Any) -> str:
    """Pretty-printed JSON for <pre> blocks (params, signatures, checks)."""
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _label(value: Any) -> str:
    """Enum value -> display text: 'review_pending' -> 'review pending'."""
    return str(value).replace("_", " ")


def _what_broke(incident: dict[str, Any], stages: dict[str, Any]) -> str | None:
    """The trigger in plain English; change incidents read the change."""
    if incident.get("kind") == "change":
        triage = stages.get("triage") or []
        service = triage[-1]["affected_service"] if triage else incident["service"]
        pr = incident.get("linked_pr")
        return f"Change to {service}" + (f" (PR {str(pr).lstrip('#')})" if pr else "")
    if not stages.get("trigger"):
        return None
    signal = stages["trigger"][0]
    sig = signal.get("signature") or {}
    service = incident["service"]
    if signal["source"] == "jenkins" and sig.get("job"):
        stage = f" at stage {sig['stage']}" if sig.get("stage") else ""
        return f"Jenkins job {sig['job']} failed{stage}"
    if signal["source"] == "kubernetes":
        reason = sig.get("waiting_reason") or sig.get("terminated_reason") or sig.get("reason")
        if reason:
            target = (
                f" on {sig['kind']} {sig['name']}" if sig.get("kind") and sig.get("name") else ""
            )
            return f"Kubernetes reported {service} as {reason}{target}"
        if sig.get("symptom"):
            return f"Kubernetes reported {service}: {_label(sig['symptom'])}"
    detail = ", ".join(f"{k} {v}" for k, v in sig.items() if isinstance(v, str | int))
    return f"{signal['source']} signal on {service}" + (f" ({detail})" if detail else "")


def _why(incident: dict[str, Any], stages: dict[str, Any]) -> str | None:
    """The latest diagnosis; change incidents show the triage verdict."""
    if incident.get("kind") == "change":
        triage = stages.get("triage") or []
        if not triage:
            return None
        t = triage[-1]
        rationale = t["rationale"]
        if t["owner"] and t["owner"] in rationale:
            return rationale[:1].upper() + rationale[1:]
        return f"Owner {t['owner'] or 'unresolved'}. {rationale}"
    hypotheses = stages.get("hypothesis") or []
    if not hypotheses:
        return None
    h = hypotheses[-1]
    return f"{h['claim']} (confidence {h['confidence']:.2f})"


def _did(incident: dict[str, Any], stages: dict[str, Any]) -> dict[str, Any] | None:
    """The latest real execution, or why nothing ran."""
    executions = stages.get("execution") or []
    primary = [e for e in executions if not e.get("compensates_id")]
    if primary:
        e = primary[-1]
        text = f"{e['action']}: {e['result']}"
        detail = (e.get("detail") or "").removeprefix(f"{e['action']} ")
        if detail:
            text += f" - {detail}"
        out = e.get("output") or {}
        if out.get("branch"):
            text += f" (branch {out['branch']})"
        if e.get("decided_by"):
            text += f" - approved by {e['decided_by']}"
        if len(primary) < len(executions):
            text += " - undone after verification failed"
        row: dict[str, Any] = {"text": text}
        url = out.get("pr_url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            row["link"] = url
        return row
    status = incident["status"]
    pending = [a for a in stages.get("approval") or [] if a["status"] == "pending"]
    reviews = [r for r in stages.get("reviews") or [] if r["status"] == "pending"]
    if status == "awaiting_approval" or pending:
        action = f" of {pending[-1]['action']}" if pending else ""
        return {"text": f"Nothing executed - waiting for approval{action}"}
    if status == "review_pending" or reviews:
        who = f" with {reviews[-1]['reviewer']}" if reviews else ""
        return {"text": f"Nothing executed - review pending{who}"}
    if status == "approved":
        return {"text": "Review approved - deploy not started yet"}
    if status == "deploying":
        return {"text": "Review approved - waiting for the deploy to report"}
    if status == "escalated":
        return {"text": "Nothing executed - escalated to a human"}
    if stages.get("trigger") and status not in ("received",):
        return {"text": "Nothing executed yet"}
    return None


def _outcome(incident: dict[str, Any], stages: dict[str, Any]) -> str:
    """Incident status plus the latest verification verdict or the cause."""
    text = _label(incident["status"]).capitalize()
    if incident.get("escalation_cause"):
        # The cause leads ("Escalated: ...") - the card is where it is shown.
        text += f": {incident['escalation_cause']}"
    verifications = stages.get("verification") or []
    if verifications:
        v = verifications[-1]
        text += f" - verification {v['outcome']}"
        if v.get("detail"):
            text += f": {v['detail']}"
    return text


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    """The incident page's headline card: four plain-English rows built
    from the provenance record. A row whose stage has no record is left
    out; the outcome always exists."""
    incident, stages = record["incident"], record["stages"]
    did = _did(incident, stages)
    rows: list[dict[str, Any]] = [
        {"label": "What broke", "text": _what_broke(incident, stages)},
        {"label": "Why", "text": _why(incident, stages)},
        {"label": "What Helix did", **(did or {"text": None})},
        {"label": "Outcome", "text": _outcome(incident, stages)},
    ]
    return {"status": incident["status"], "rows": [r for r in rows if r["text"]]}


templates.env.filters["label"] = _label
templates.env.filters["dt"] = _fmt_dt
templates.env.filters["age"] = _age
templates.env.filters["jsonpp"] = _jsonpp


def get_db_session() -> Iterator[Session]:
    """Request-scoped session, same contract as helix.api.get_db_session -
    duplicated here rather than imported so this module never imports the
    api module (api.py imports THIS one to mount the UI). Tests override
    both dependencies to point at the same engine."""
    with session_scope() as session:
        yield session


DbSession = Annotated[Session, Depends(get_db_session)]

router = APIRouter(include_in_schema=False)


def _incident_total(session: Session) -> int:
    """Incident count for the sidebar nav on every page."""
    return session.scalar(select(func.count()).select_from(models.Incident)) or 0


def _pending_count(session: Session) -> int:
    """Pending approvals plus pending reviews - everything waiting on a
    human in the decision queue - for the nav badge on every page."""
    approvals = session.scalar(
        select(func.count())
        .select_from(models.Approval)
        .where(models.Approval.status == models.ApprovalStatus.PENDING)
    )
    reviews = session.scalar(
        select(func.count())
        .select_from(models.ReviewRequest)
        .where(models.ReviewRequest.status == models.ApprovalStatus.PENDING)
    )
    return (approvals or 0) + (reviews or 0)


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> Response:
    """Root is the public landing page for a signed-out visitor when sign-in
    is on; everyone else lands on the dashboard - the UI lives under
    /ui/ so the JSON API keeps its /incidents and /approvals paths unchanged."""
    from helix import auth  # auth imports this module's templates

    if auth.shows_landing(request):
        return templates.TemplateResponse(request, "landing.html", {})
    return RedirectResponse("/ui/", status_code=307)


def _samples_enabled() -> bool:
    """The test incident needs recorded evidence to raise a failure from."""
    return get_settings().evidence_source == "fixtures"


HOME_OPEN_LIMIT = 6
HOME_ACTIVITY_LIMIT = 8
HOME_PENDING_LIMIT = 50
HOME_WINDOW = timedelta(days=30)
HOME_TOOLS = (("slack", "Slack"), ("jira", "Jira"), ("github", "GitHub"))


def _utc(value: datetime) -> datetime:
    """A naive datetime (SQLite) read as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _display_name(login: str) -> str:
    """The registry's display name for a login, else the login itself."""
    person = load_registry().person(login)
    return (person.name if person else "") or login


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _duration(seconds: float) -> str:
    """'42s', '5m', '1h 12m', '2d 3h' - the two largest units that apply."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours, minutes = divmod(seconds // 60, 60)
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(seconds // 3600, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _home_stats(session: Session, now: datetime) -> dict[str, Any]:
    """Incidents created in the last 30 days: how many opened, recovered and
    escalated, the share of the ones that finished that recovered, and the
    median time from creation to the transition into recovered."""
    since = now - HOME_WINDOW
    rows = session.execute(
        select(models.Incident.id, models.Incident.status, models.Incident.created_at).where(
            models.Incident.created_at >= since
        )
    ).all()
    recovered_at = dict(
        session.execute(
            select(models.Transition.incident_id, func.min(models.Transition.created_at))
            .join(models.Incident, models.Incident.id == models.Transition.incident_id)
            .where(
                models.Incident.created_at >= since,
                models.Transition.to_status == IncidentStatus.RECOVERED,
            )
            .group_by(models.Transition.incident_id)
        ).all()
    )
    recovered = sum(1 for r in rows if r.status == IncidentStatus.RECOVERED)
    finished = sum(1 for r in rows if r.status in TERMINAL_STATUSES)
    taken = [
        (_utc(recovered_at[r.id]) - _utc(r.created_at)).total_seconds()
        for r in rows
        if r.id in recovered_at
    ]
    median = _median(taken)
    return {
        "opened": len(rows),
        "recovered": recovered,
        "escalated": sum(1 for r in rows if r.status == IncidentStatus.ESCALATED),
        "recovered_pct": round(100 * recovered / finished) if finished else None,
        "median_recover": _duration(median) if median is not None else "-",
    }


def _needs_you(session: Session, user: str | None) -> tuple[list[dict], list[dict]]:
    """Pending approvals and reviews, split into (mine, waiting on someone
    else) by the real decider rules in state.deciders. With nobody signed in
    nothing is mine."""
    pending = models.ApprovalStatus.PENDING
    items: list[dict[str, Any]] = []
    for a in session.scalars(
        select(models.Approval)
        .where(models.Approval.status == pending)
        .order_by(models.Approval.created_at, models.Approval.id)
        .limit(HOME_PENDING_LIMIT)
    ):
        owners = approval_deciders(a.incident)
        items.append(
            {
                "kind": "Approval",
                "incident_id": a.incident.incident_id,
                "what": a.action,
                "created_at": a.created_at,
                "href": f"/ui/approvals#approval-{a.id}",
                "waiting_on": ", ".join(_display_name(o) for o in owners) or "no registered owner",
                "mine": bool(user) and approval_refusal(a.incident, user) is None,
            }
        )
    for r in session.scalars(
        select(models.ReviewRequest)
        .where(models.ReviewRequest.status == pending)
        .order_by(models.ReviewRequest.created_at, models.ReviewRequest.id)
        .limit(HOME_PENDING_LIMIT)
    ):
        items.append(
            {
                "kind": "Review",
                "incident_id": r.incident.incident_id,
                "what": f"Change to {r.incident.service}",
                "created_at": r.created_at,
                "href": f"/ui/approvals#review-{r.id}",
                "waiting_on": _display_name(r.reviewer),
                "mine": bool(user) and review_refusal(r, user) is None,
            }
        )
    items.sort(key=lambda i: _utc(i["created_at"]))
    return [i for i in items if i["mine"]], [i for i in items if not i["mine"]]


def _open_incidents(session: Session) -> list[dict[str, Any]]:
    """The newest non-terminal incidents with what each is waiting on, in
    the words of the incident page's summary card."""
    rows = session.scalars(
        select(models.Incident)
        .where(models.Incident.status.not_in(TERMINAL_STATUSES))
        .order_by(models.Incident.created_at.desc(), models.Incident.id.desc())
        .limit(HOME_OPEN_LIMIT)
    ).all()
    out = []
    for i in rows:
        record = assemble_provenance(session, i.incident_id)
        waiting = None
        if record is not None:
            waiting = (_did(record["incident"], record["stages"]) or {}).get("text")
            waiting = waiting or _outcome(record["incident"], record["stages"])
        out.append(
            {
                "incident_id": i.incident_id,
                "service": i.service,
                "status": i.status.value,
                "waiting": waiting,
                "created_at": i.created_at,
            }
        )
    return out


def _recent_activity(session: Session) -> list[dict[str, Any]]:
    """The latest status changes across all incidents, newest first."""
    rows = session.execute(
        select(models.Transition, models.Incident.incident_id)
        .join(models.Incident, models.Incident.id == models.Transition.incident_id)
        .order_by(models.Transition.created_at.desc(), models.Transition.id.desc())
        .limit(HOME_ACTIVITY_LIMIT)
    ).all()
    return [
        {
            "incident_id": incident_id,
            "from_status": t.from_status.value if t.from_status else None,
            "to_status": t.to_status.value,
            "cause": t.cause,
            "created_at": t.created_at,
        }
        for t, incident_id in rows
    ]


_SLACK_ID = re.compile(r"[A-Z0-9]{2,32}")
_SLACK_TS = re.compile(r"\d{1,12}\.\d{1,12}")
_JIRA_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")


def _slack_url(team_id: Any, channel: Any, ts: Any = None) -> str | None:
    """app.slack.com link to a channel, or to the thread whose parent message
    has `ts` (the format Slack's own "copy link" opens in the web client).
    None unless every id looks like one, so a link is never half-built."""
    if not (isinstance(team_id, str) and _SLACK_ID.fullmatch(team_id)):
        return None
    if not (isinstance(channel, str) and _SLACK_ID.fullmatch(channel)):
        return None
    base = f"https://app.slack.com/client/{team_id}/{channel}"
    if ts is None:
        return base
    return f"{base}/thread/{channel}-{ts}" if _SLACK_TS.fullmatch(str(ts)) else None


def _web_url(value: Any) -> str | None:
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) else None


def _tool_link(provider: str, record: Any, session: Session) -> dict[str, Any] | None:
    """Where "Open" goes for a connected tool, or None when nothing is
    configured yet. Read from stored settings only - no provider is called."""
    settings = record.settings
    if provider == "slack":
        url = _slack_url(settings.get("team_id"), settings.get("channel"))
        if url is None:
            return None
        return {"url": url, "label": settings.get("channel_name") or "your channel"}
    if provider == "jira":
        site, key = _web_url(settings.get("site_url")), settings.get("project_key")
        if not site or not isinstance(key, str) or not _JIRA_KEY.fullmatch(key):
            return None
        # /browse/KEY opens the project for any project type (the board URL
        # /jira/software/projects/KEY only exists for software projects).
        site_name = settings.get("site_name")
        label = f"{key} - {site_name}" if site_name else key
        return {"url": f"{site.rstrip('/')}/browse/{key}", "label": label}
    repo = pr_bot.resolve(get_settings(), session.get(models.PRBotSettings, 1)).repo
    if not _REPO.fullmatch(repo or ""):
        return None
    return {"url": f"https://github.com/{repo}", "label": repo}


_PICK = {"slack": "Pick a channel", "jira": "Pick a project", "github": "Pick a repository"}


def _tools(session: Session) -> list[dict[str, Any]]:
    """Connected state of each tool Helix works through, with the link out
    to its configured channel, project or repository."""
    out = []
    for provider, name in HOME_TOOLS:
        record = connections.active_connection(session, provider)
        out.append(
            {
                "id": provider,
                "name": name,
                "connected": record is not None,
                "account": record.account if record else None,
                "link": _tool_link(provider, record, session) if record else None,
                "pick": _PICK[provider],
            }
        )
    return out


def _incident_links(session: Session, incident: dict[str, Any], stages: dict[str, Any]) -> list:
    """External links for an incident's header: its Slack thread, Jira work
    item and pull request. Each is left out when its data is missing."""
    from integrations.outbox import slack_thread_for

    links: list[dict[str, str]] = []
    pk = session.scalar(
        select(models.Incident.id).where(models.Incident.incident_id == incident["incident_id"])
    )
    thread = slack_thread_for(session, pk) if pk is not None else None
    slack = connections.active_connection(session, "slack")
    if thread and slack:
        channel = thread[1] or slack.settings.get("channel")
        url = _slack_url(slack.settings.get("team_id"), channel, thread[0])
        if url:
            links.append({"label": "Slack thread", "url": url})
    for item in stages.get("work_items") or []:
        url = _web_url(item.get("external_url"))
        if item.get("provider") == "jira" and url:
            links.append({"label": f"Jira {item.get('external_key') or ''}".strip(), "url": url})
    pr = (_did(incident, stages) or {}).get("link")  # the summary's own PR lookup
    if pr:
        links.append({"label": "Pull request", "url": pr})
    return links


@router.get("/ui/", response_class=HTMLResponse)
def home(request: Request, session: DbSession) -> HTMLResponse:
    """The dashboard: what needs you, what is going on, is Helix working."""
    now = datetime.now(UTC)
    user = getattr(request.state, "user", None)
    mine, waiting = _needs_you(session, user)
    tools = _tools(session)
    total = _incident_total(session)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "active": "home",
            "incident_total": total,
            "pending_count": _pending_count(session),
            "user": user,
            "name": _display_name(user) if user else None,
            "today": now.strftime("%A, %d %B %Y"),
            "samples_enabled": _samples_enabled(),
            "mine": mine,
            "waiting": waiting,
            "open_incidents": _open_incidents(session),
            "stats": _home_stats(session, now),
            "activity": _recent_activity(session),
            "tools": tools,
            "fresh": total == 0 and not any(t["connected"] for t in tools),
        },
    )


@router.get("/ui/incidents", response_class=HTMLResponse)
def incident_list(request: Request, session: DbSession) -> HTMLResponse:
    """All incidents, newest first - the operator's entry point."""
    incidents = session.scalars(
        select(models.Incident).order_by(
            models.Incident.created_at.desc(), models.Incident.id.desc()
        )
    ).all()
    rows = [
        {
            "incident_id": i.incident_id,
            "service": i.service,
            "environment": i.environment,
            "severity": i.severity,
            "status": i.status.value,
            "attempt_count": i.attempt_count,
            "signal_count": len(i.signals),
            "created_at": i.created_at,
        }
        for i in incidents
    ]
    stats = {
        "total": len(rows),
        "recovered": sum(1 for r in rows if r["status"] == IncidentStatus.RECOVERED),
        "escalated": sum(1 for r in rows if r["status"] == IncidentStatus.ESCALATED),
        "open": sum(1 for i in incidents if i.status not in TERMINAL_STATUSES),
    }
    return templates.TemplateResponse(
        request,
        "incidents.html",
        {
            "active": "incidents",
            "incidents": rows,
            "stats": stats,
            "incident_total": len(rows),
            "pending_count": _pending_count(session),
            "samples_enabled": _samples_enabled(),
        },
    )


@router.post("/ui/samples")
def start_sample(session: DbSession) -> RedirectResponse:
    """Start a test incident, or point at the one running."""
    if not _samples_enabled():
        raise HTTPException(status_code=404)
    incident_id, already_running = demo.start_sample(session)
    suffix = "?busy=1" if already_running else ""
    return RedirectResponse(f"/ui/incidents/{incident_id}{suffix}", status_code=303)


@router.get("/ui/incidents/{incident_id}", response_class=HTMLResponse)
def incident_detail(
    request: Request,
    incident_id: str,
    session: DbSession,
    busy: bool = False,
) -> HTMLResponse:
    """One incident's header plus its full decision-provenance timeline.

    `terminal` tells app.js whether to poll GET /incidents/{id} - the JSON
    read endpoint - for status changes (non-terminal incidents only)."""
    record = assemble_provenance(session, incident_id)
    if record is None:
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "active": "incidents",
                "incident_total": _incident_total(session),
                "pending_count": _pending_count(session),
                "code": 404,
                "message": f"no such incident: {incident_id}",
            },
            status_code=404,
        )
    incident = record["incident"]
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "incident.html",
        {
            "default_actor": settings.operator,
            "token_required": bool(settings.intake_token),
            "active": "incidents",
            "incident_total": _incident_total(session),
            "pending_count": _pending_count(session),
            "i": incident,
            "stages": record["stages"],
            "summary": _summary(record),
            "ext_links": _incident_links(session, incident, record["stages"]),
            "transitions": record["transitions"],
            "terminal": incident["status"] in TERMINAL_STATUSES,
            "busy": busy,
        },
    )


def _approval_row(row: models.Approval) -> dict[str, Any]:
    """Plain-dict view of an approvals row (session detaches on render);
    risk tier resolves through the candidate FK when the row carries one."""
    return {
        "id": row.id,
        "incident_id": row.incident.incident_id,
        "incident_status": row.incident.status.value,
        "action": row.action,
        "params": row.params,
        "fingerprint": row.fingerprint,
        "status": row.status.value,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at,
        "note": row.note,
        "created_at": row.created_at,
        "fingerprint_short": row.fingerprint[:16],
        "risk_tier": row.candidate.risk_tier if row.candidate is not None else None,
    }


def _review_row(row: models.ReviewRequest) -> dict[str, Any]:
    """Plain-dict view of a review_requests row for the decision queue."""
    return {
        "id": row.id,
        "incident_id": row.incident.incident_id,
        "incident_status": row.incident.status.value,
        "service": row.incident.service,
        "owner": row.incident.owner,
        "reviewer": row.reviewer,
        "rationale": row.rationale,
        "status": row.status.value,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at,
        "note": row.note,
        "created_at": row.created_at,
    }


@router.get("/ui/approvals", response_class=HTMLResponse)
def approvals_queue(request: Request, session: DbSession) -> HTMLResponse:
    """The decision queue: approvals, then change-lifecycle reviews. Each
    lists pending rows first (oldest waiting first), then decided rows
    newest-first with who decided and when.

    `default_actor` prefills the operator-name field (HELIX_OPERATOR);
    `token_required` shows the intake-token field only when the decision
    endpoints are actually gated."""
    rows = [
        _approval_row(r)
        for r in session.scalars(select(models.Approval).order_by(models.Approval.id)).all()
    ]
    pending = [r for r in rows if r["status"] == models.ApprovalStatus.PENDING]
    decided = [r for r in reversed(rows) if r["status"] != models.ApprovalStatus.PENDING]
    review_rows = [
        _review_row(r)
        for r in session.scalars(select(models.ReviewRequest).order_by(models.ReviewRequest.id))
    ]
    pending_reviews = [r for r in review_rows if r["status"] == models.ApprovalStatus.PENDING]
    decided_reviews = [
        r for r in reversed(review_rows) if r["status"] != models.ApprovalStatus.PENDING
    ]
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "active": "approvals",
            "incident_total": _incident_total(session),
            "pending_count": len(pending) + len(pending_reviews),
            "pending": pending,
            "decided": decided,
            "pending_reviews": pending_reviews,
            "decided_reviews": decided_reviews,
            "default_actor": settings.operator,
            "token_required": bool(settings.intake_token),
        },
    )


def register_ui(app: FastAPI) -> None:
    """Mount the operator UI on `app`: static assets at /static plus the
    page routes. Called at the bottom of helix/api.py so `helix serve`
    exposes the UI and the JSON API from one process."""
    app.mount("/static", StaticFiles(directory=str(UI_DIR / "static")), name="static")
    app.include_router(router)
    from helix import connections_ui  # imports this module, so only here

    app.include_router(connections_ui.router)
    from helix import team_ui

    app.include_router(team_ui.router)
    app.include_router(team_ui.link_router)
    from helix import services_ui

    app.include_router(services_ui.router)
