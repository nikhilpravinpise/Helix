"""Operator UI (subphase 1.12, D11): server-rendered pages on the same
FastAPI app as the JSON API - no node toolchain, no build step.

Pages (the JSON API keeps its own routes, unchanged):

    GET /                      redirect to the incident list
    GET /ui/                   incident list (status, severity, age)
    GET /ui/incidents/{id}     incident header + full provenance timeline
                               (trigger -> evidence -> hypothesis ->
                               candidates -> decision -> validation ->
                               approval -> execution -> verification),
                               assembled by state.provenance; unknown id
                               renders a 404 page
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
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from helix.config import get_settings
from state import models
from state.db import session_scope
from state.machine import TERMINAL_STATUSES
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


def _pending_count(session: Session) -> int:
    """Pending approvals count for the nav badge on every page."""
    return (
        session.scalar(
            select(func.count())
            .select_from(models.Approval)
            .where(models.Approval.status == models.ApprovalStatus.PENDING)
        )
        or 0
    )


@router.get("/", response_class=HTMLResponse)
def index() -> RedirectResponse:
    """Root lands on the incident list - the UI lives under /ui/ so the
    JSON API keeps its /incidents and /approvals paths unchanged."""
    return RedirectResponse("/ui/", status_code=307)


@router.get("/ui/", response_class=HTMLResponse)
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
    return templates.TemplateResponse(
        request,
        "incidents.html",
        {"active": "incidents", "incidents": rows, "pending_count": _pending_count(session)},
    )


@router.get("/ui/incidents/{incident_id}", response_class=HTMLResponse)
def incident_detail(request: Request, incident_id: str, session: DbSession) -> HTMLResponse:
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
                "pending_count": _pending_count(session),
                "code": 404,
                "message": f"no such incident: {incident_id}",
            },
            status_code=404,
        )
    incident = record["incident"]
    return templates.TemplateResponse(
        request,
        "incident.html",
        {
            "active": "incidents",
            "pending_count": _pending_count(session),
            "i": incident,
            "stages": record["stages"],
            "transitions": record["transitions"],
            "terminal": incident["status"] in TERMINAL_STATUSES,
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


@router.get("/ui/approvals", response_class=HTMLResponse)
def approvals_queue(request: Request, session: DbSession) -> HTMLResponse:
    """The approval queue: pending rows first (oldest waiting first), then
    decided rows newest-first with who decided and when.

    `default_actor` prefills the operator-name field (HELIX_OPERATOR);
    `token_required` shows the intake-token field only when the decision
    endpoints are actually gated."""
    rows = [
        _approval_row(r)
        for r in session.scalars(select(models.Approval).order_by(models.Approval.id)).all()
    ]
    pending = [r for r in rows if r["status"] == models.ApprovalStatus.PENDING]
    decided = [r for r in reversed(rows) if r["status"] != models.ApprovalStatus.PENDING]
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "active": "approvals",
            "pending_count": len(pending),
            "pending": pending,
            "decided": decided,
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
