"""Team page and invites (future_plans.md "Team and services").

    GET  /ui/team                              members, pending invites, invite form
    POST /ui/team/invite                       invite a GitHub login as admin or member
    POST /ui/team/invites/{id}/resend          new link and a fresh 7 days
    POST /ui/team/invites/{id}/cancel          withdraw the invite
    POST /ui/team/people/{login}/role          make admin / make member
    POST /ui/team/people/{login}/rename        change the display name
    POST /ui/team/people/{login}/remove        take them off the team
    GET  /invite/{token}                       public: what an invite link opens

Everyone signed in can read the page; every POST needs an admin
(helix/auth.py `require_admin`). A rule that is broken (state/team.py
TeamError) comes back as a notice in a short-lived cookie rather than in the
URL, so a crafted link cannot put words in the page's mouth.
"""

import math
import re
from datetime import UTC, datetime
from urllib.parse import quote, unquote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from helix import auth
from helix.connections_ui import _posted
from helix.ui import DbSession, _incident_total, _pending_count, templates
from state import models, team
from state.team import TeamError

FLASH_COOKIE = "helix_team_flash"
_LOGIN = re.compile(r"[A-Za-z0-9-]{1,39}")

router = APIRouter(include_in_schema=False, dependencies=[auth.AdminOnly])

# notice code -> text; {login} is the person it is about.
_NOTICES = {
    "invited": "Invited {login}. Copy the invite link and send it to them.",
    "resent": "Made a new invite link for {login}, good for 7 days.",
    "cancelled": "Cancelled the invite for {login}.",
    "role-changed": "Changed the role for {login}.",
    "renamed": "Renamed {login}.",
    "removed": "Removed {login} from the team.",
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
    try:
        row = team.invite(
            session,
            await _posted(request, "login"),
            await _posted(request, "role"),
            getattr(request.state, "user", None) or "Helix",
            await _posted(request, "email"),
        )
    except TeamError as error:
        return _refused(request, error)
    return _back("invited", row.login)


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
