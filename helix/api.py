"""Helix HTTP API: signal intake, incident reads, and approval decisions.

The intake POSTs and `helix intake` both call state.intake.intake_signal,
so the benchmark harness never depends on this server running (D6, push
model). Auth: when HELIX_INTAKE_TOKEN is set, intake POSTs and approval
decisions must carry `Authorization: Bearer <token>`; unset means
local-only trust. The read endpoints are unauthenticated on that same
local-only basis.

Subphase 1.8 adds the approval endpoints: `GET /approvals` lists the
gate's pending/decided rows, and `POST /approvals/{id}/approve|reject`
record the human decision (actor in the body) and resume the suspended
workflow run in-process - the decision is durable in Postgres first, the
resume is best-effort (`helix run` can always drive it later).

Subphase 1.9 adds `GET /incidents/{id}/provenance` - the assembled
decision-provenance record (state/provenance.py), the JSON surface the
Phase 1.12 operator timeline will render.

Subphase 1.12 mounts the operator UI (helix/ui.py): server-rendered pages
under /ui/ plus static assets at /static, with / redirecting to the
incident list. The JSON routes below are unchanged.

Subphase 2.2 adds `POST /slack/interactions` - Slack's signed webhook for
interactive payloads: Approve/Reject button presses land here and are
turned into the same durable record_decision + resume the /approvals
endpoints perform. Slack is a door to the approvals row, never its store.

Subphase 2.6 adds the review-gate surface: `GET /reviews` lists the
change-lifecycle review_requests rows, and
`POST /reviews/{id}/approve|reject` record the reviewer's decision
(durable first) and resume the run - rejection drives
REJECTED -> FIX_REQUIRED, approval drives REVIEW_PENDING -> APPROVED.

`helix serve` runs this app under uvicorn at HELIX_API_BIND:HELIX_API_PORT.
"""

import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated
from urllib.parse import parse_qsl

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.logging import get_logger
from helix.ui import register_ui
from integrations.slack.signature import verify_signature
from state import models
from state.approvals import ApprovalDecidedError, record_decision
from state.db import session_scope
from state.intake import IntakeResult, Signal, SignalSource, intake_signal
from state.machine import IncidentStatus

app = FastAPI(
    title="Helix",
    description="Evidence-constrained DevOps recovery - intake and incident state",
)


@app.middleware("http")
async def sign_in_gate(request: Request, call_next):
    """With a GitHub OAuth app configured, every route needs a signed-in,
    allowed user or the intake token (helix/auth.py). Off otherwise."""
    from helix import auth

    refused = auth.gate(request)
    if refused is not None:
        return refused
    return await call_next(request)


# HEAD too: uptime monitors check with it, and a 405 reads as "down".
@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz() -> dict[str, bool]:
    """Liveness for the hosting platform; open even behind the gate."""
    return {"ok": True}


def get_db_session() -> Iterator[Session]:
    """Request-scoped session; commits on clean exit, rolls back on error."""
    with session_scope() as session:
        yield session


DbSession = Annotated[Session, Depends(get_db_session)]


def _signed_in(request: Request) -> str | None:
    """The GitHub login the sign-in gate attached (helix/auth.py), if any."""
    return getattr(request.state, "user", None)


def require_intake_token(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> None:
    """Bearer-token gate for intake POSTs. No-op when HELIX_INTAKE_TOKEN is
    unset (local-only trust), or for a user signed in with GitHub: the
    dashboard's own buttons carry the session, not the token. Missing
    header -> 401, wrong token -> 403."""
    token = get_settings().intake_token
    if not token or _signed_in(request):
        return
    if authorization is None:
        raise HTTPException(
            status_code=401,
            detail="missing Authorization: Bearer <token> header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, credential = authorization.partition(" ")
    # compare_digest: a plain != leaks how much of the token matched.
    if scheme.lower() != "bearer" or not hmac.compare_digest(credential, token):
        raise HTTPException(status_code=403, detail="invalid intake token")


def _intake(signal: Signal, expected_source: SignalSource, session: Session) -> IntakeResult:
    """Shared endpoint body: the path pins the source, so a signal whose
    declared source disagrees with the endpoint is a client error."""
    if signal.source != expected_source:
        raise HTTPException(
            status_code=422,
            detail=f"signal source {signal.source!r} does not match endpoint "
            f"{expected_source.value!r}",
        )
    return intake_signal(session, signal)


def _maybe_run(
    run: bool, session: Session, result: IntakeResult, background: BackgroundTasks
) -> None:
    """The intake -> agent hook (D6 direct call, wired in 1.5): POSTs with
    `?run=true` drive the workflow graph in-process after the signal is
    recorded. The signal is committed first so the runner's own sessions
    see it.

    The run is a background task, not part of the request: a run takes
    minutes, and intake's callers are time-boxed (Jenkins' post-failure
    curl gives up after 10s), so running inline failed the callers
    `?run=true` exists for. A run failure is logged but never fails the
    intake - the incident is already durable and `helix run` can drive it
    later."""
    if not run:
        return
    session.commit()
    background.add_task(_run_logged, result.incident_id)


def _run_logged(incident_id: str) -> None:
    from agent.runner import run_incident

    try:
        run_incident(incident_id)
    except Exception as exc:  # the run's outcome lives in workflow_runs
        get_logger().warning("intake run hook failed", incident_id=incident_id, error=str(exc))


@app.post("/intake/kubernetes", response_model=IntakeResult)
def post_intake_kubernetes(
    signal: Signal,
    session: DbSession,
    background: BackgroundTasks,
    _: Annotated[None, Depends(require_intake_token)],
    run: bool = False,
) -> IntakeResult:
    """Receive a Kubernetes failure signal (e.g. from scripts/emit-signal.sh).
    `?run=true` also drives the incident's workflow in-process."""
    result = _intake(signal, SignalSource.KUBERNETES, session)
    _maybe_run(run, session, result, background)
    return result


@app.post("/intake/jenkins", response_model=IntakeResult)
def post_intake_jenkins(
    signal: Signal,
    session: DbSession,
    background: BackgroundTasks,
    _: Annotated[None, Depends(require_intake_token)],
    run: bool = False,
) -> IntakeResult:
    """Receive a Jenkins build-failure signal (Jenkinsfile post{failure}).
    `?run=true` also drives the incident's workflow in-process."""
    result = _intake(signal, SignalSource.JENKINS, session)
    _maybe_run(run, session, result, background)
    return result


class IncidentOut(BaseModel):
    """The canonical incident record, read side."""

    model_config = ConfigDict(from_attributes=True)

    incident_id: str
    service: str
    environment: str
    severity: str
    status: IncidentStatus
    attempt_count: int
    linked_pr: str | None
    linked_deployment: str | None
    fingerprint: str
    created_at: datetime
    updated_at: datetime
    signal_count: int = 0


class SignalOut(BaseModel):
    """One stored signal row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    environment: str
    severity: str
    signature: dict
    raw: dict
    created_at: datetime


class TransitionOut(BaseModel):
    """One persisted status transition. from_status is null only on the
    creation row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    from_status: IncidentStatus | None
    to_status: IncidentStatus
    cause: str
    created_at: datetime


class IncidentDetail(IncidentOut):
    """Incident plus its attached signals and transition timeline."""

    signals: list[SignalOut] = []
    transitions: list[TransitionOut] = []


@app.get("/incidents", response_model=list[IncidentOut])
def list_incidents(session: DbSession) -> list[IncidentOut]:
    """All incidents, newest first."""
    incidents = session.scalars(
        select(models.Incident).order_by(
            models.Incident.created_at.desc(), models.Incident.id.desc()
        )
    ).all()
    out = []
    for incident in incidents:
        item = IncidentOut.model_validate(incident)
        item.signal_count = len(incident.signals)
        out.append(item)
    return out


@app.get("/incidents/{incident_id}", response_model=IncidentDetail)
def get_incident(incident_id: str, session: DbSession) -> IncidentDetail:
    """One incident by its INC-* id, with signals and the transition
    timeline - the base of the 1.9 provenance record and the 1.12 UI."""
    incident = session.scalar(
        select(models.Incident).where(models.Incident.incident_id == incident_id)
    )
    if incident is None:
        raise HTTPException(status_code=404, detail=f"no such incident: {incident_id}")
    detail = IncidentDetail.model_validate(incident)
    detail.signal_count = len(detail.signals)
    return detail


@app.get("/incidents/{incident_id}/provenance")
def get_incident_provenance(incident_id: str, session: DbSession) -> dict:
    """The incident's decision-provenance record (subphase 1.9): the
    ordered trigger -> evidence -> hypothesis -> candidates -> decision ->
    validation -> approval -> execution -> verification timeline,
    assembled from stored rows by state/provenance.py. 404 on an unknown
    incident id."""
    from state.provenance import assemble_provenance

    record = assemble_provenance(session, incident_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no such incident: {incident_id}")
    return record


class ApprovalOut(BaseModel):
    """One approvals row - the gate's pending or decided record."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    incident_id: str  # the incident's INC-* external id
    action: str
    params: dict
    fingerprint: str
    status: models.ApprovalStatus
    decided_by: str | None
    decided_at: datetime | None
    note: str | None
    created_at: datetime


def _approval_out(row: models.Approval) -> ApprovalOut:
    return ApprovalOut(
        id=row.id,
        incident_id=row.incident.incident_id,
        action=row.action,
        params=row.params,
        fingerprint=row.fingerprint,
        status=row.status,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        note=row.note,
        created_at=row.created_at,
    )


@app.get("/approvals", response_model=list[ApprovalOut])
def list_approvals(
    session: DbSession,
    status: models.ApprovalStatus | None = None,
    incident_id: str | None = None,
) -> list[ApprovalOut]:
    """Approval rows, newest first; filterable by status / incident."""
    query = select(models.Approval).order_by(models.Approval.id.desc())
    if status is not None:
        query = query.where(models.Approval.status == status)
    if incident_id is not None:
        query = query.join(models.Incident).where(models.Incident.incident_id == incident_id)
    return [_approval_out(row) for row in session.scalars(query).all()]


class ApprovalDecisionIn(BaseModel):
    """The decision body: WHO approved or rejected. The approver identity
    is required - the audit record has no anonymous decisions."""

    actor: str = Field(min_length=1, max_length=128)
    note: str = ""
    #: The dashboard's "Approve my own change": honoured only for an owner
    #: or admin who wrote the change (state/deciders.py). Slack never sets it.
    self_approve: bool = False


class ApprovalDecisionOut(BaseModel):
    """The decided row plus what the resumed run did. With `?wait=false` the
    run resumes after the response: `scheduled` is true, `resumed` false and
    `status` is the incident's status at decision time."""

    approval: ApprovalOut
    resumed: bool
    scheduled: bool = False
    status: str | None = None  # incident status after the resume
    run_status: str | None = None
    run_error: str | None = None


def _decider(request: Request, body: "ApprovalDecisionIn") -> str:
    """Who the decision is recorded as: the signed-in GitHub user when
    there is one - a typed name cannot override a session - else the
    body's actor (CLI, machine clients with the intake token)."""
    return _signed_in(request) or body.actor


def _schedule_resume(background: BackgroundTasks, incident_id: str) -> None:
    """Resume after the response, until the run parks again - the dashboard
    shows "Approving..." instead of a button frozen for the whole run."""
    from agent.runner import run_until_parked

    background.add_task(_resume_logged, run_until_parked, incident_id)


def _status_now(incident: models.Incident) -> str:
    return getattr(incident.status, "value", str(incident.status))


def _decide_approval(
    approval_id: int,
    approved: bool,
    body: ApprovalDecisionIn,
    session: Session,
    actor: str,
    background: BackgroundTasks | None = None,
) -> ApprovalDecisionOut:
    """Record the decision on a pending row and resume the suspended run.

    The decision commits before `run_incident` fires so the runner's own
    sessions see it; a resume failure is reported in the response, never
    raised - the decision itself is already durable and `helix run` can
    drive the incident later."""
    approval = session.get(models.Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=f"no such approval: {approval_id}")
    try:
        record_decision(
            session, approval, approved, actor, body.note, self_approve=body.self_approve
        )
    except ApprovalDecidedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    incident_id = approval.incident.incident_id
    session.commit()
    if background is not None:
        _schedule_resume(background, incident_id)
        return ApprovalDecisionOut(
            approval=_approval_out(approval),
            resumed=False,
            scheduled=True,
            status=_status_now(approval.incident),
        )

    resumed = False
    status = run_status = run_error = None
    from agent.runner import run_incident

    try:
        result = run_incident(incident_id)
        resumed = True
        status, run_status = result.status, result.run_status.value
        run_error = result.error
    except Exception as exc:  # decision is durable; the run is retryable
        run_error = f"{type(exc).__name__}: {exc}"
        get_logger().warning("approval resume failed", incident_id=incident_id, error=run_error)
    return ApprovalDecisionOut(
        approval=_approval_out(approval),
        resumed=resumed,
        status=status,
        run_status=run_status,
        run_error=run_error,
    )


@app.post("/approvals/{approval_id}/approve", response_model=ApprovalDecisionOut)
def approve_approval(
    request: Request,
    response: Response,
    background: BackgroundTasks,
    approval_id: int,
    body: ApprovalDecisionIn,
    session: DbSession,
    _: Annotated[None, Depends(require_intake_token)],
    wait: bool = True,
) -> ApprovalDecisionOut:
    """Approve the pending row and resume the suspended run."""
    if not wait:
        response.status_code = 202
    return _decide_approval(
        approval_id, True, body, session, _decider(request, body), None if wait else background
    )


@app.post("/approvals/{approval_id}/reject", response_model=ApprovalDecisionOut)
def reject_approval(
    request: Request,
    response: Response,
    background: BackgroundTasks,
    approval_id: int,
    body: ApprovalDecisionIn,
    session: DbSession,
    _: Annotated[None, Depends(require_intake_token)],
    wait: bool = True,
) -> ApprovalDecisionOut:
    """Reject the pending row and resume: the incident replans with the
    rejected fingerprint excluded."""
    if not wait:
        response.status_code = 202
    return _decide_approval(
        approval_id, False, body, session, _decider(request, body), None if wait else background
    )


def _slack_action(
    actor: str, action: dict, session: Session, timeout_s: float, background: BackgroundTasks
) -> dict:
    """One button press -> one approval decision plus a scheduled resume.

    Mirrors _decide_approval but never raises HTTPException: Slack needs a
    fast 200 ack, and a stale button or a retried delivery must be an
    acknowledged note in the body, not an error Slack keeps retrying."""
    action_id = str(action.get("action_id") or "")
    kind, _, raw_id = action_id.partition(":")
    if kind in ("helix_approve", "helix_reject") and raw_id.isdigit():
        approval_id = int(raw_id)
        approval = session.get(models.Approval, approval_id)
        if approval is None:
            return {"approval_id": approval_id, "note": "no such approval - stale message"}
        try:
            record_decision(
                session,
                approval,
                kind == "helix_approve",
                actor,
                f"via Slack ({action_id})",
                timeout_s=timeout_s,
            )
        except ApprovalDecidedError as exc:
            return {"approval_id": approval_id, "note": str(exc)}
        row_id = approval_id
        id_key = "approval_id"
    elif kind in ("helix_review_approve", "helix_review_reject") and raw_id.isdigit():
        # 2.6: the same button surface for the change-lifecycle review
        # gate - the decision lands on the durable review_requests row.
        from state.reviews import ReviewDecidedError
        from state.reviews import record_decision as record_review_decision

        review_id = int(raw_id)
        review = session.get(models.ReviewRequest, review_id)
        if review is None:
            return {"review_id": review_id, "note": "no such review - stale message"}
        try:
            record_review_decision(
                session,
                review,
                kind == "helix_review_approve",
                actor,
                f"via Slack ({action_id})",
                timeout_s=timeout_s,
            )
        except ReviewDecidedError as exc:
            return {"review_id": review_id, "note": str(exc)}
        row_id = review_id
        id_key = "review_id"
    else:
        return {"action_id": action_id, "note": "unrecognized action"}
    incident_id = (
        approval.incident.incident_id if id_key == "approval_id" else review.incident.incident_id
    )
    session.commit()
    result: dict = {
        id_key: row_id,
        "decision": (approval.status if id_key == "approval_id" else review.status).value,
    }
    # The resume runs after the ack - Slack's interaction deadline is 3s
    # and a full run (verify windows, replan hops) can take minutes.
    # Holding the HTTP response for it made Slack retry and mark the
    # click failed while the work still happened; the decision is
    # already durable, so scheduling the run is strictly honest.
    # Until parked, not one run: an approved change advances one lifecycle
    # stage per run, so a single run left it at `approved` instead of
    # carrying it on to `deploying` - the click looked like it did nothing.
    from agent.runner import run_until_parked

    background.add_task(_resume_logged, run_until_parked, incident_id)
    result["resumed"] = "scheduled"
    return result


def _resume_logged(run_incident, incident_id: str) -> None:  # type: ignore[no-untyped-def]
    """Background-task wrapper: a resume failure lands in the log, never
    in a response Slack already got."""
    try:
        run_incident(incident_id)
    except Exception as exc:  # the decision is durable; the run is retryable
        get_logger().warning(
            "decision resume failed",
            incident_id=incident_id,
            error=f"{type(exc).__name__}: {exc}",
        )


@app.post("/slack/interactions")
async def slack_interactions(
    request: Request, session: DbSession, background: BackgroundTasks
) -> dict:
    """Slack's interactive webhook (subphase 2.2).

    Slack signs every POST with HELIX_SLACK_SIGNING_SECRET; the signature
    is the whole authentication (Slack cannot carry the intake bearer
    token), so an unset secret closes the surface - 503, secure by
    default. Slack requires the ack inside 3s: the decision commits
    first, the run resume is scheduled as a background task (a full run
    can take minutes), and retries/stale buttons come back as
    200-with-note rather than errors."""
    settings = get_settings()
    if not settings.slack_signing_secret:
        raise HTTPException(status_code=503, detail="slack signing secret not configured")
    body = await request.body()
    timestamp = request.headers.get("x-slack-request-timestamp", "")
    signature = request.headers.get("x-slack-signature", "")
    if not verify_signature(settings.slack_signing_secret, timestamp, body, signature):
        raise HTTPException(status_code=401, detail="invalid slack signature")

    # Interactive payloads arrive form-urlencoded in a `payload` field;
    # url_verification arrives as a raw JSON body.
    if (request.headers.get("content-type") or "").startswith("application/x-www-form-urlencoded"):
        raw = dict(parse_qsl(body.decode("utf-8"))).get("payload")
        if raw is None:
            raise HTTPException(status_code=400, detail="missing payload field")
    else:
        raw = body
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="malformed slack payload") from None

    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}
    return handle_slack_interaction(payload, session, background)


@app.post("/webhooks/github")
async def github_webhook(
    request: Request, session: DbSession, background: BackgroundTasks
) -> Response:
    """The GitHub App's webhook: real activity on a registered repo reaches
    the dashboard. The X-Hub-Signature-256 HMAC is the whole authentication
    (GitHub cannot carry the intake bearer token), so an unset secret closes
    the surface (404). Pull requests are ingested and a run is scheduled
    after the response (GitHub times out at 10s); check runs are only
    recorded - on the hosted Helix every diagnosis reads one pinned
    fixture, so a check-run incident is never run."""
    settings = get_settings()
    if not settings.github_webhook_secret:
        raise HTTPException(status_code=404, detail="not found")
    body = await request.body()
    expected = (
        "sha256="
        + hmac.new(settings.github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    )
    if not hmac.compare_digest(expected, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(status_code=401, detail="invalid github signature")

    event = request.headers.get("x-github-event", "")
    delivery = request.headers.get("x-github-delivery", "")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="malformed github payload") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="malformed github payload")
    action = str(payload.get("action") or "")

    def reply(status: int, content: dict, result: str) -> Response:
        get_logger().info(
            "github webhook", delivery=delivery, github_event=event, action=action, result=result
        )
        return JSONResponse(content, status_code=status)

    if event == "ping":
        return reply(200, {"ok": True, "ping": True}, "ping")
    if event not in ("pull_request", "check_run"):
        return reply(202, {"ok": True, "ignored": event}, "ignored event")

    from agent.ops.registry import load_registry
    from events.adapters.github import check_run_events, pull_request_events
    from events.ingest import ingest_event

    repo = str((payload.get("repository") or {}).get("full_name") or "").lower()
    # Services sharing one repo: the first by name takes the delivery.
    service = next(
        (
            name
            for name, entry in sorted(load_registry().services.items())
            if repo and entry.repo.lower() == repo
        ),
        None,
    )
    if service is None:
        return reply(202, {"ok": True, "ignored": "repository not registered"}, "unregistered")

    if event == "pull_request":
        events = pull_request_events(payload, service=service)
    else:
        check = payload.get("check_run") or {}
        events = (
            check_run_events(
                {
                    "ref": check.get("head_sha"),
                    "items": [
                        {
                            "name": check.get("name"),
                            "status": check.get("status"),
                            "conclusion": check.get("conclusion"),
                            "url": check.get("html_url"),
                            "started_at": check.get("started_at"),
                            "completed_at": check.get("completed_at"),
                        }
                    ],
                },
                service=service,
            )
            if action == "completed"
            else []
        )
    if not events:
        return reply(202, {"ok": True, "ignored": action or event}, "ignored action")

    results = [ingest_event(session, e) for e in events]
    # Commit before the response so the scheduled run sees the incident.
    session.commit()
    if event == "pull_request":
        from agent.runner import run_until_parked

        for result in results:
            if not result.duplicate:
                background.add_task(_resume_logged, run_until_parked, result.incident_id)
    return reply(
        200,
        {"ok": True, "incidents": sorted({r.incident_id for r in results})},
        "duplicate" if all(r.duplicate for r in results) else "ingested",
    )


def _slack_person(member: str | None) -> str | None:
    """The registry handle whose `people.<handle>.slack` is this Slack member
    id, or None when nobody is linked to it."""
    from agent.ops.registry import load_registry

    for handle, person in load_registry().people.items():
        if member and person.slack == member:
            return handle
    return None


def _slack_handle(user: dict) -> str:
    """The registry handle of the Slack user who clicked. Someone not linked
    keeps their Slack name, which the decision rules then refuse with a reason."""
    member = user.get("id")
    return _slack_person(member) or user.get("username") or user.get("name") or member or "slack"


def handle_slack_interaction(payload: dict, session: Session, background: BackgroundTasks) -> dict:
    """Decide the button presses in one authenticated Slack interaction.

    Shared by the HTTP route above (authenticated by the request signature)
    and `helix slack-socket` (authenticated by its Socket Mode connection),
    so a click is handled identically however it arrived. The decision is
    committed here; the workflow resume is left on `background` for the
    caller to run after acknowledging Slack."""
    ptype = payload.get("type")
    if ptype != "block_actions":
        return {"ok": True, "ignored": ptype}
    settings = get_settings()
    user = payload.get("user") or {}
    actor = _slack_handle(user)
    timeout = settings.slack_approval_timeout
    if timeout is None:
        timeout = settings.approval_timeout
    decisions = [
        _slack_action(actor, a, session, timeout, background) for a in payload.get("actions") or []
    ]
    verdicts = [
        _VERDICT[("review" if "review_id" in d else "approval", d["decision"])]
        for d in decisions
        if d.get("decision") in ("approved", "rejected")
    ]
    if verdicts and payload.get("response_url"):
        # Show the outcome where the click happened: the decided message
        # loses its buttons and gains the verdict. Without this a recorded
        # click looked like nothing happened. Sent after the ack.
        who = f"<@{user['id']}>" if user.get("id") else actor
        background.add_task(
            _replace_with_verdict,
            str(payload["response_url"]),
            (payload.get("message") or {}).get("blocks") or [],
            f"{verdicts[0]} by {who}",
        )
    refusals = [d["note"] for d in decisions if "decision" not in d and d.get("note")]
    if refusals and payload.get("response_url"):
        # A refused click (not the owner or reviewer, already decided,
        # timed out) must say so to the person who clicked, or it looks
        # like nothing happened. Only they see it; the card is unchanged.
        background.add_task(_tell_clicker, str(payload["response_url"]), refusals[0])
    if payload.get("response_url") and user.get("id") and _slack_person(user["id"]) is None:
        # Helix cannot tell who this is: offer a private, signed link that
        # binds their Slack account to their Helix account once they open it
        # while signed in (helix/team_ui.py link-slack).
        link = _slack_link_url(payload, user)
        if link:
            background.add_task(
                _send_ephemeral,
                str(payload["response_url"]),
                f"Helix doesn't know which teammate you are yet. Link your Slack account: {link}",
            )
    return {"ok": True, "decisions": decisions}


#: How long a Slack self-link stays valid.
_LINK_MINUTES = 15


def _slack_link_url(payload: dict, user: dict) -> str | None:
    """The signed self-link for an unlinked Slack member; None (logged) when
    Helix has no public URL or session secret to sign one with."""
    from helix import auth

    base = (get_settings().public_url or "").rstrip("/")
    try:
        token = auth._sign(
            {
                # Session cookies share the signing key; the purpose keeps a
                # link token from standing in for any other signed value.
                "purpose": "slack-link",
                "slack_id": user["id"],
                "slack_name": user.get("username") or user.get("name") or user["id"],
                "team": (payload.get("team") or {}).get("id") or user.get("team_id") or "",
                "exp": time.time() + _LINK_MINUTES * 60,
            }
        )
    except RuntimeError:
        base = ""
    if not base:
        get_logger().warning(
            "slack self-link unavailable: set HELIX_PUBLIC_URL and the session secret"
        )
        return None
    return f"{base}{auth.LINK_SLACK_PATH}?t={token}"


def _send_ephemeral(response_url: str, text: str) -> None:
    """A private note to the person who clicked; a failure is only logged."""
    try:
        httpx.post(
            response_url,
            json={"response_type": "ephemeral", "replace_original": False, "text": text},
            timeout=10.0,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        get_logger().warning("slack link note failed", error=str(exc))


def _tell_clicker(response_url: str, text: str) -> None:
    """An ephemeral reply to the person who clicked."""
    from integrations.slack.format import escape

    try:
        httpx.post(
            response_url,
            json={
                "response_type": "ephemeral",
                "replace_original": False,
                "text": f"Not recorded: {escape(text)}",
            },
            timeout=10.0,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        get_logger().warning("slack refusal note failed", error=str(exc))


#: How a recorded decision reads on the message it was made from.
_VERDICT = {
    ("review", "approved"): "✅ Approved",
    ("review", "rejected"): "✋ Changes requested",
    ("approval", "approved"): "✅ Approved",
    ("approval", "rejected"): "⛔ Rejected",
}


def _replace_with_verdict(response_url: str, blocks: list, verdict: str) -> None:
    """Replace the clicked message: same content, buttons gone, verdict
    added. A failure is logged - the decision is already durable."""
    kept = [block for block in blocks if block.get("type") != "actions"]
    kept.append({"type": "context", "elements": [{"type": "mrkdwn", "text": verdict}]})
    try:
        httpx.post(
            response_url,
            json={"replace_original": True, "text": verdict, "blocks": kept},
            timeout=10.0,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        get_logger().warning("slack verdict update failed", error=str(exc))


class ReviewOut(BaseModel):
    """One review_requests row - the 2.6 reviewer gate's record."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    incident_id: str
    reviewer: str
    rationale: str
    status: models.ApprovalStatus
    decided_by: str | None
    decided_at: datetime | None
    note: str | None
    created_at: datetime


def _review_out(row: models.ReviewRequest) -> ReviewOut:
    return ReviewOut(
        id=row.id,
        incident_id=row.incident.incident_id,
        reviewer=row.reviewer,
        rationale=row.rationale,
        status=row.status,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        note=row.note,
        created_at=row.created_at,
    )


@app.get("/reviews", response_model=list[ReviewOut])
def list_reviews(
    session: DbSession,
    status: models.ApprovalStatus | None = None,
    incident_id: str | None = None,
) -> list[ReviewOut]:
    """Review requests, newest first; filterable by status / incident."""
    query = select(models.ReviewRequest).order_by(models.ReviewRequest.id.desc())
    if status is not None:
        query = query.where(models.ReviewRequest.status == status)
    if incident_id is not None:
        query = query.join(models.Incident).where(models.Incident.incident_id == incident_id)
    return [_review_out(row) for row in session.scalars(query).all()]


class ReviewDecisionOut(BaseModel):
    """The decided review row plus what the resumed run did (see
    ApprovalDecisionOut for `?wait=false`)."""

    review: ReviewOut
    resumed: bool
    scheduled: bool = False
    status: str | None = None
    run_status: str | None = None
    run_error: str | None = None


def _decide_review(
    review_id: int,
    approved: bool,
    body: ApprovalDecisionIn,
    session: Session,
    actor: str,
    background: BackgroundTasks | None = None,
) -> ReviewDecisionOut:
    """The review-side twin of `_decide_approval`: record the decision on
    the pending review_requests row (durable first), then best-effort
    resume so the change lifecycle moves off REVIEW_PENDING."""
    from state.reviews import ReviewDecidedError
    from state.reviews import record_decision as record_review_decision

    review = session.get(models.ReviewRequest, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail=f"no such review: {review_id}")
    try:
        record_review_decision(
            session, review, approved, actor, body.note, self_approve=body.self_approve
        )
    except ReviewDecidedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    incident_id = review.incident.incident_id
    session.commit()
    if background is not None:
        _schedule_resume(background, incident_id)
        return ReviewDecisionOut(
            review=_review_out(review),
            resumed=False,
            scheduled=True,
            status=_status_now(review.incident),
        )

    resumed = False
    status = run_status = run_error = None
    from agent.runner import run_incident

    try:
        result = run_incident(incident_id)
        resumed = True
        status, run_status = result.status, result.run_status.value
        run_error = result.error
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {exc}"
        get_logger().warning("review resume failed", incident_id=incident_id, error=run_error)
    return ReviewDecisionOut(
        review=_review_out(review),
        resumed=resumed,
        status=status,
        run_status=run_status,
        run_error=run_error,
    )


@app.post("/reviews/{review_id}/approve", response_model=ReviewDecisionOut)
def approve_review(
    request: Request,
    response: Response,
    background: BackgroundTasks,
    review_id: int,
    body: ApprovalDecisionIn,
    session: DbSession,
    _: Annotated[None, Depends(require_intake_token)],
    wait: bool = True,
) -> ReviewDecisionOut:
    """Approve the pending review request and resume: the change proceeds
    toward APPROVED -> DEPLOYING."""
    if not wait:
        response.status_code = 202
    return _decide_review(
        review_id, True, body, session, _decider(request, body), None if wait else background
    )


@app.post("/reviews/{review_id}/reject", response_model=ReviewDecisionOut)
def reject_review(
    request: Request,
    response: Response,
    background: BackgroundTasks,
    review_id: int,
    body: ApprovalDecisionIn,
    session: DbSession,
    _: Annotated[None, Depends(require_intake_token)],
    wait: bool = True,
) -> ReviewDecisionOut:
    """Reject the pending review request and resume: the change drives
    REJECTED -> FIX_REQUIRED awaiting an updated commit."""
    if not wait:
        response.status_code = 202
    return _decide_review(
        review_id, False, body, session, _decider(request, body), None if wait else background
    )


# Subphase 1.12 (D11): mount the server-rendered operator UI - / redirects
# to /ui/ (incident list), detail and approvals pages live under /ui/,
# assets under /static. ui.py keeps its own session dependency so there is
# no import cycle back into this module.
register_ui(app)

from helix.auth import router as _auth_router  # noqa: E402

app.include_router(_auth_router)
