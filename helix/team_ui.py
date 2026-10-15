"""Team page and invites (future_plans.md "Team and services").

    GET  /ui/team                              members, pending invites, invite form
    POST /ui/team/invite                       invite a GitHub login as admin or member
    POST /ui/team/invites/{id}/resend          new link and a fresh 7 days
    POST /ui/team/invites/{id}/cancel          withdraw the invite
    POST /ui/team/people/{login}/role          make admin / make member
    POST /ui/team/people/{login}/rename        change the display name
    POST /ui/team/people/{login}/find-slack    find them in Slack by email
    POST /ui/team/people/{login}/remove        take them off the team
    GET  /invite/{token}                       public: what an invite link opens
    GET  /ui/team/link-slack?t=...             confirm linking your Slack (any signed-in role)
    POST /ui/team/link-slack                   bind it

Everyone signed in can read the page; every POST needs an admin
(helix/auth.py `require_admin`), except linking your own Slack account. A rule
that is broken (state/team.py TeamError) comes back as a notice in a
short-lived cookie rather than in the URL, so a crafted link cannot put words
in the page's mouth.
"""

import math
import re
from datetime import UTC, datetime
from urllib.parse import quote, unquote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from helix import auth, slack_people
from helix.connections_ui import _posted
from helix.ui import DbSession, _incident_total, _pending_count, templates
from state import models, team
from state.team import TeamError

FLASH_COOKIE = "helix_team_flash"
_LOGIN = re.compile(r"[A-Za-z0-9-]{1,39}")

router = APIRouter(include_in_schema=False, dependencies=[auth.AdminOnly])

RECONNECT = "Reconnect Slack on the Connections page so Helix can find people by email."

# notice code -> text; {login} is the person it is about.
_NOTICES = {
    "invited": "Invited {login}. Copy the invite link and send it to them.",
    "resent": "Made a new invite link for {login}, good for 7 days.",
    "cancelled": "Cancelled the invite for {login}.",
    "role-changed": "Changed the role for {login}.",
    "renamed": "Renamed {login}.",
    "removed": "Removed {login} from the team.",
    "invited-messaged": (
        "Invited {login}. Found them in Slack and sent them the invite. "
        "You can still copy the invite link."
    ),
    "invited-dm-failed": (
        "Invited {login}. Found them in Slack but could not message them. "
        "Copy the invite link and send it to them."
    ),
    "invited-not-found": (
        "Invited {login}. Helix could not find that email in Slack. "
        "Copy the invite link and send it to them."
    ),
    "invited-reconnect": (
        "Invited {login}. " + RECONNECT + " Until then, copy the invite link and send it to them."
    ),
    "invited-slack-error": (
        "Invited {login}. Slack did not answer, so Helix could not look them up. "
        "Copy the invite link and send it to them."
    ),
    "slack-found": "Linked {login} to their Slack account.",
    "slack-not-found": "Helix could not find that email in Slack, so nothing was changed.",
    "slack-not-connected": (
        "Slack is not connected, so Helix cannot look people up. "
        "Connect it on the Connections page first."
    ),
    "slack-reconnect": RECONNECT,
    "slack-error": "Slack did not answer, so nothing was changed.",
}


def _back(code: str, login: str = "") -> RedirectResponse:
    suffix = f"&login={quote(login)}" if _LOGIN.fullmatch(login) else ""
    return RedirectResponse(f"/ui/team?notice={code}{suffix}", status_code=303)


def _refused(request: Request, error: TeamError) -> RedirectResponse:
    response = RedirectResponse("/ui/team", status_code=303)
    response.set_cookie(
        FLASH_COOKIE,
        quote(str(error)),
        max_age=60,
        httponly=True,
        secure=auth._secure(request),
        samesite="lax",
        path="/ui/team",
    )
    return response


def _expires_in(expires_at: datetime) -> str:
    seconds = (expires_at - datetime.now(UTC)).total_seconds()
    if seconds <= 0:
        return "Expired"
    days = math.ceil(seconds / 86400)
    if seconds >= 86400:
        return f"Expires in {days} day{'' if days == 1 else 's'}"
    hours = int(seconds // 3600)
    if hours:
        return f"Expires in {hours} hour{'' if hours == 1 else 's'}"
    return "Expires in under an hour"


@router.get("/ui/team", response_class=HTMLResponse)
def team_page(request: Request, session: DbSession, notice: str = "", login: str = "") -> Response:
    people = list(session.scalars(select(models.Person).order_by(models.Person.created_at)))
    names = {p.login: p.name or p.login for p in people}
    public = auth._public_url(request)
    invites = [
        {
            "id": inv.id,
            "login": inv.login,
            "role": inv.role,
            "email": inv.email,
            "invited_by": names.get(inv.invited_by, inv.invited_by),
            "created_at": team._aware(inv.created_at),
            "expires": _expires_in(team._aware(inv.expires_at)),
            "link": f"{public}/invite/{inv.token}",
        }
        for inv in team.pending_invites(session)
    ]
    flash = request.cookies.get(FLASH_COOKIE)
    if flash:
        found: tuple[str, str] | None = ("bad", unquote(flash))
    elif notice in _NOTICES:
        found = ("ok", _NOTICES[notice].format(login=login if _LOGIN.fullmatch(login) else "them"))
    else:
        found = None
    response = templates.TemplateResponse(
        request,
        "team.html",
        {
            "active": "team",
            "people": people,
            "invites": invites,
            "can_manage": auth.may_manage(request, session),
            "slack_reconnect": RECONNECT if slack_people.needs_reconnect(session) else None,
            "me": getattr(request.state, "user", None),
            "notice": found,
            "incident_total": _incident_total(session),
            "pending_count": _pending_count(session),
        },
    )
    if flash:
        response.delete_cookie(FLASH_COOKIE, path="/ui/team")
    return response


@router.post("/ui/team/invite")
async def invite(request: Request, session: DbSession) -> Response:
    user = getattr(request.state, "user", None)
    try:
        row = team.invite(
            session,
            await _posted(request, "login"),
            await _posted(request, "role"),
            user or "Helix",
            await _posted(request, "email"),
        )
    except TeamError as error:
        return _refused(request, error)
    return _back(_match_invitee(request, session, row, user), row.login)


def _match_invitee(request: Request, session, row: models.Invitation, user: str | None) -> str:
    """Best effort, and never undoes the invite: look the invited email up in
    Slack, remember the member id and DM the invite. Returns the notice code."""
    if not row.email:
        return "invited"
    found = slack_people.find_by_emails(session, [row.email])
    if found.status == "found":
        row.slack_id = found.slack_id
        inviter = session.get(models.Person, user) if user else None
        who = (inviter.name if inviter and inviter.name else user) or "Helix"
        role = "an admin" if row.role == "admin" else "a member"
        sent = slack_people.send_dm(
            session,
            found.slack_id,
            f"{who} invited you to the team on Helix as {role}. "
            f"Accept: {auth._public_url(request)}/invite/{row.token}",
        )
        return "invited-messaged" if sent else "invited-dm-failed"
    return {
        "not_found": "invited-not-found",
        "reconnect": "invited-reconnect",
        "error": "invited-slack-error",
    }.get(found.status, "invited")


@router.post("/ui/team/invites/{invite_id}/resend")
def resend(request: Request, session: DbSession, invite_id: int) -> Response:
    try:
        row = team.resend(session, invite_id)
    except TeamError as error:
        return _refused(request, error)
    return _back("resent", row.login)


@router.post("/ui/team/invites/{invite_id}/cancel")
def cancel(request: Request, session: DbSession, invite_id: int) -> Response:
    row = session.get(models.Invitation, invite_id)
    try:
        team.cancel(session, invite_id)
    except TeamError as error:
        return _refused(request, error)
    return _back("cancelled", row.login if row else "")


@router.post("/ui/team/people/{login}/role")
async def role(request: Request, session: DbSession, login: str) -> Response:
    try:
        team.set_role(session, login, await _posted(request, "role"))
    except TeamError as error:
        return _refused(request, error)
    return _back("role-changed", login)


@router.post("/ui/team/people/{login}/rename")
async def rename(request: Request, session: DbSession, login: str) -> Response:
    try:
        team.rename(session, login, await _posted(request, "name"))
    except TeamError as error:
        return _refused(request, error)
    return _back("renamed", login)


@router.post("/ui/team/people/{login}/find-slack")
async def find_slack(request: Request, session: DbSession, login: str) -> Response:
    email = (await _posted(request, "email")).strip()
    try:
        if not email:
            raise TeamError("Enter an email.")
        if session.get(models.Person, login) is None:
            raise TeamError(f"{login} is not on the team.")
        found = slack_people.find_by_emails(session, [email])
        if found.slack_id:
            team.link_slack(session, login, found.slack_id, email)
    except TeamError as error:
        return _refused(request, error)
    code = {
        "found": "slack-found",
        "not_found": "slack-not-found",
        "not_connected": "slack-not-connected",
        "reconnect": "slack-reconnect",
    }.get(found.status, "slack-error")
    return _back(code, login)


@router.post("/ui/team/people/{login}/remove")
def remove(request: Request, session: DbSession, login: str) -> Response:
    try:
        team.remove_person(session, login)
    except TeamError as error:
        return _refused(request, error)
    return _back("removed", login)


@router.get("/invite/{token}", response_class=HTMLResponse)
def invite_page(request: Request, session: DbSession, token: str) -> Response:
    """Public: what an invite link opens. Says who invited you and how to
    accept (sign in with the invited GitHub login); an unknown token is a 404."""
    row = session.scalar(select(models.Invitation).where(models.Invitation.token == token))
    if row is None:
        return templates.TemplateResponse(request, "invite.html", {"state": "unknown"}, 404)
    if row.accepted_at is not None:
        state = "accepted"
    elif row.cancelled_at is not None:
        state = "cancelled"
    elif not team.is_pending(row):
        state = "expired"
    else:
        state = "pending"
    inviter = session.get(models.Person, row.invited_by)
    return templates.TemplateResponse(
        request,
        "invite.html",
        {
            "state": state,
            "role": row.role,
            "inviter": (inviter.name if inviter and inviter.name else row.invited_by),
            "login": row.login,
        },
    )


# ---- linking your own Slack account ------------------------------------------

#: Any signed-in role may link their own Slack account, so this is not on the
#: admin-only router above.
link_router = APIRouter(include_in_schema=False)

_LINK_BAD = "That link is not valid or has expired. Click the button in Slack again for a new one."


def _link_claims(token: str) -> dict | None:
    try:
        claims = auth._unsign(token)
    except RuntimeError:
        return None
    return (
        claims
        if claims
        and claims.get("purpose") == "slack-link"
        and isinstance(claims.get("slack_id"), str)
        and claims["slack_id"]
        else None
    )


def _link_view(request: Request, session, token: str, *, bind: bool) -> Response:
    """Verify the token and the visitor; on `bind`, link. GET only shows the
    confirm card and changes nothing."""
    claims = _link_claims(token)
    user = getattr(request.state, "user", None)
    state, message, status = "confirm", "", 200
    person = session.get(models.Person, user) if user else None
    if claims is None:
        state, message, status = "bad", _LINK_BAD, 400
    elif user is None:
        state, message, status = (
            "bad",
            "Sign-in is not turned on, so Helix cannot tell who you are.",
            400,
        )
    elif person is None:
        state, message, status = (
            "bad",
            f"{user} is not on the team yet. Ask an admin to invite you.",
            400,
        )
    else:
        holder = session.scalar(
            select(models.Person).where(
                models.Person.slack_id == claims["slack_id"], models.Person.login != user
            )
        )
        if holder is not None:
            state, message, status = (
                "bad",
                "That Slack account is already linked to someone else - ask an admin.",
                400,
            )
        elif person.slack_id and person.slack_id != claims["slack_id"]:
            state, message, status = (
                "bad",
                "Your account is linked to a different Slack account - ask an admin to change it.",
                400,
            )
        elif bind:
            team.link_slack(session, user, claims["slack_id"])
            state = "done"
    return templates.TemplateResponse(
        request,
        "link_slack.html",
        {
            "active": "team",
            "state": state,
            "message": message,
            "token": token,
            "slack_name": (claims or {}).get("slack_name", ""),
            "login": user,
            "incident_total": _incident_total(session),
            "pending_count": _pending_count(session),
        },
        status,
    )


@link_router.get(auth.LINK_SLACK_PATH, response_class=HTMLResponse)
def link_slack_page(request: Request, session: DbSession, t: str = "") -> Response:
    return _link_view(request, session, t, bind=False)


@link_router.post(auth.LINK_SLACK_PATH, response_class=HTMLResponse)
async def link_slack(request: Request, session: DbSession) -> Response:
    return _link_view(request, session, await _posted(request, "t"), bind=True)
