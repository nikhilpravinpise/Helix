"""Team page and invites (future_plans.md "Team and services").

    GET  /ui/team                              members, pending invites, invite form
    POST /ui/team/invite                       invite a GitHub login as owner, admin or member
    POST /ui/team/invites/{id}/resend          new link and a fresh 7 days
    POST /ui/team/invites/{id}/cancel          withdraw the invite
    POST /ui/team/people/{login}/role          make owner / admin / member
    POST /ui/team/people/{login}/main-owner    hand main ownership to a co-owner
    POST /ui/team/people/{login}/rename        change the display name
    POST /ui/team/people/{login}/find-slack    find them in Slack by email
    POST /ui/team/people/{login}/remove        take them off the team
    GET  /invite/{token}                       public: what an invite link opens
    GET  /ui/team/link-slack?t=...             confirm linking your Slack (any signed-in role)
    POST /ui/team/link-slack                   bind it

Everyone signed in can read the page; every POST needs an admin or an owner
(helix/auth.py `require_admin`), except renaming yourself and linking your own
Slack account. Who may change whom is state/team.py's rank rules. A rule
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

from helix import auth, github_users, invite_email, slack_people
from helix.connections_ui import _posted
from helix.ui import DbSession, _incident_total, _pending_count, templates
from integrations import email
from state import models, team
from state.team import TeamError

FLASH_COOKIE = "helix_team_flash"
_LOGIN = re.compile(r"[A-Za-z0-9-]{1,39}")
_MAIL_CODE = re.compile(r"[a-z0-9_]{1,30}")

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
    "main-owner": "{login} is now the main owner.",
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
    "slack-picked": "Sent {login} the invite in Slack. You can still copy the invite link.",
    "slack-pick-dm-failed": (
        "Linked {login} to that Slack account but could not message them. "
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


def _back(code: str, login: str = "", mail: tuple[str, str] = ("none", "")) -> RedirectResponse:
    suffix = f"&login={quote(login)}" if _LOGIN.fullmatch(login) else ""
    if mail[0] != "none":
        suffix += f"&mail={mail[0]}" + (f"&mailerr={quote(mail[1])}" if mail[1] else "")
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


def _notice_text(session, notice: str, login: str, name: str, mail: str, mailerr: str) -> str:
    """The notice with the invite email's outcome added. When the email went
    out, the "copy the link" advice of the Slack outcomes no longer applies."""
    text = _NOTICES[notice].format(login=name)
    row = next((i for i in team.pending_invites(session) if i.login == login), None)
    if mail not in ("sent", "off", "failed") or row is None or not row.email:
        return text
    if mail == "sent":
        if notice == "resent":
            return f"{text} Emailed the new link to {row.email}."
        slack = " Also sent it to them in Slack." if notice == "invited-messaged" else ""
        return f"Invited {name}. Emailed the invite link to {row.email}.{slack}"
    if notice == "invited":
        text = f"Invited {name}."
    if mail == "off":
        return f"{text} Email is not set up, so copy the invite link and send it to them."
    code = mailerr if _MAIL_CODE.fullmatch(mailerr) else "error"
    return f"{text} Could not email them ({code}); copy the invite link and send it to them."


@router.get("/ui/team", response_class=HTMLResponse)
def team_page(
    request: Request,
    session: DbSession,
    notice: str = "",
    login: str = "",
    mail: str = "",
    mailerr: str = "",
) -> Response:
    people = list(session.scalars(select(models.Person).order_by(models.Person.created_at)))
    names = {p.login: p.name or p.login for p in people}
    public = auth._public_url(request)
    actor = auth.actor_for(request, session)
    grantable = team.grantable_roles(actor)
    me = actor.login if actor else None
    main = next((p.login for p in people if p.main_owner), None)
    slack_on = slack_people.connected(session)
    invites = [
        {
            "id": inv.id,
            "login": inv.login,
            "role": inv.role,
            "email": inv.email,
            "invited_by": names.get(inv.invited_by, inv.invited_by),
            "created_at": team._aware(inv.created_at),
            "expires": _expires_in(team._aware(inv.expires_at)),
            "emailed_at": team._aware(inv.emailed_at) if inv.emailed_at else None,
            "link": f"{public}/invite/{inv.token}",
            "can_manage": team.may_manage_invite(actor, inv),
            "can_pick": slack_on and not inv.slack_id and team.may_manage_invite(actor, inv),
        }
        for inv in team.pending_invites(session)
    ]
    flash = request.cookies.get(FLASH_COOKIE)
    if flash:
        found: tuple[str, str] | None = ("bad", unquote(flash))
    elif notice in _NOTICES:
        who = login if _LOGIN.fullmatch(login) else "them"
        # Someone on the team is named as everywhere else; an invitee by login.
        person = session.get(models.Person, who) if who != "them" else None
        name = (person.name if person else "") or who
        found = ("ok", _notice_text(session, notice, who, name, mail, mailerr))
    else:
        found = None
    response = templates.TemplateResponse(
        request,
        "team.html",
        {
            "active": "team",
            "people": [
                {
                    "p": p,
                    "can_act": team.may_act_on(actor, p),
                    "can_rename": team.may_rename(actor, p),
                    "roles": [r for r in grantable if r != p.role],
                    "can_make_main": actor is not None
                    and actor.login == main
                    and p.role == "owner"
                    and not p.main_owner,
                }
                for p in people
            ],
            "grantable": grantable,
            "invites": invites,
            "can_manage": auth.may_manage(request, session),
            "slack_reconnect": RECONNECT if slack_people.needs_reconnect(session) else None,
            "email_on": email.is_configured(),
            "me": me,
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
    login = (await _posted(request, "login")).strip()
    typed_email = await _posted(request, "email")
    try:
        # Only a well-formed login is worth asking GitHub about.
        profile = github_users.lookup(login) if team._GITHUB_LOGIN.fullmatch(login) else None
        if profile is False:
            raise TeamError(f"There is no GitHub user called {login}.")
        row = team.invite(
            session,
            profile.login if profile else login,
            await _posted(request, "role"),
            user or "Helix",
            typed_email or (profile.email if profile else None),
            actor=auth.actor_for(request, session),
        )
    except TeamError as error:
        return _refused(request, error)
    emails = [e for e in (typed_email.strip(), profile.email if profile else None) if e]
    code = _match_invitee(request, session, row, user, list(dict.fromkeys(emails)))
    mail = invite_email.send_invite(
        session, row, _display_name(session, user), auth._public_url(request)
    )
    return _back(code, row.login, mail)


def _display_name(session, login: str | None) -> str:
    person = session.get(models.Person, login) if login else None
    return (person.name if person and person.name else login) or "Helix"


def _dm_invite(request: Request, session, row: models.Invitation, user: str | None) -> bool:
    """DM the invite link to the member id on the row."""
    role = {"owner": "an owner", "admin": "an admin"}.get(row.role, "a member")
    return slack_people.send_dm(
        session,
        row.slack_id,
        f"{_display_name(session, user)} invited you to the team on Helix as {role}. "
        f"Accept: {auth._public_url(request)}/invite/{row.token}",
    )


def _match_invitee(
    request: Request, session, row: models.Invitation, user: str | None, emails: list[str]
) -> str:
    """Best effort, and never undoes the invite: look every known email up in
    Slack (typed first, then the GitHub profile's), remember the member id and
    DM the invite. Returns the notice code."""
    if not emails:
        return "invited"
    found = slack_people.find_by_emails(session, emails)
    if found.status == "found":
        row.slack_id = found.slack_id
        return (
            "invited-messaged" if _dm_invite(request, session, row, user) else "invited-dm-failed"
        )
    return {
        "not_found": "invited-not-found",
        "reconnect": "invited-reconnect",
        "error": "invited-slack-error",
    }.get(found.status, "invited")


@router.post("/ui/team/invites/{invite_id}/resend")
def resend(request: Request, session: DbSession, invite_id: int) -> Response:
    try:
        row = team.resend(session, invite_id, actor=auth.actor_for(request, session))
    except TeamError as error:
        return _refused(request, error)
    user = getattr(request.state, "user", None)
    mail = invite_email.send_invite(
        session, row, _display_name(session, user), auth._public_url(request)
    )
    return _back("resent", row.login, mail)


@router.post("/ui/team/invites/{invite_id}/cancel")
def cancel(request: Request, session: DbSession, invite_id: int) -> Response:
    row = session.get(models.Invitation, invite_id)
    try:
        team.cancel(session, invite_id, actor=auth.actor_for(request, session))
    except TeamError as error:
        return _refused(request, error)
    return _back("cancelled", row.login if row else "")


_SLACK_ID = re.compile(r"[UW][A-Z0-9]{2,20}")


def _managed_open_invite(session, request: Request, invite_id: int) -> models.Invitation:
    row = session.get(models.Invitation, invite_id)
    if row is None or not team.is_pending(row):
        raise TeamError("That invite is no longer pending.")
    if not team.may_manage_invite(auth.actor_for(request, session), row):
        raise TeamError("You cannot change this invite.")
    return row


@router.post("/ui/team/invites/{invite_id}/slack-search", response_class=HTMLResponse)
async def slack_search(request: Request, session: DbSession, invite_id: int) -> Response:
    """Search the Slack workspace by name and show who matches, each with a
    button that picks them for this invite."""
    try:
        row = _managed_open_invite(session, request, invite_id)
    except TeamError as error:
        return _refused(request, error)
    query = (await _posted(request, "q")).strip()
    status, members = slack_people.search_members(session, query) if query else ("empty", [])
    return templates.TemplateResponse(
        request,
        "team_slack_pick.html",
        {
            "active": "team",
            "invite": row,
            "query": query,
            "status": status,
            "members": members,
            "reconnect": RECONNECT,
            "incident_total": _incident_total(session),
            "pending_count": _pending_count(session),
        },
    )


@router.post("/ui/team/invites/{invite_id}/slack-pick")
async def slack_pick(request: Request, session: DbSession, invite_id: int) -> Response:
    """Use the chosen Slack member for this invite and DM them the link."""
    slack_id = (await _posted(request, "slack_id")).strip()
    try:
        row = _managed_open_invite(session, request, invite_id)
        if not _SLACK_ID.fullmatch(slack_id):
            raise TeamError("Choose someone from the list.")
    except TeamError as error:
        return _refused(request, error)
    row.slack_id = slack_id
    session.flush()
    user = getattr(request.state, "user", None)
    sent = _dm_invite(request, session, row, user)
    return _back("slack-picked" if sent else "slack-pick-dm-failed", row.login)


@router.post("/ui/team/people/{login}/role")
async def role(request: Request, session: DbSession, login: str) -> Response:
    try:
        team.set_role(
            session, login, await _posted(request, "role"), actor=auth.actor_for(request, session)
        )
    except TeamError as error:
        return _refused(request, error)
    return _back("role-changed", login)


@router.post("/ui/team/people/{login}/rename")
async def rename(request: Request, session: DbSession, login: str) -> Response:
    try:
        team.rename(
            session, login, await _posted(request, "name"), actor=auth.actor_for(request, session)
        )
    except TeamError as error:
        return _refused(request, error)
    return _back("renamed", login)


@router.post("/ui/team/people/{login}/find-slack")
async def find_slack(request: Request, session: DbSession, login: str) -> Response:
    email = (await _posted(request, "email")).strip()
    try:
        if not email:
            raise TeamError("Enter an email.")
        person = session.get(models.Person, login)
        if person is None:
            raise TeamError(f"{login} is not on the team.")
        if not team.may_rename(auth.actor_for(request, session), person):
            raise TeamError("You cannot change this person.")
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
        team.remove_person(session, login, actor=auth.actor_for(request, session))
    except TeamError as error:
        return _refused(request, error)
    return _back("removed", login)


@router.post("/ui/team/people/{login}/main-owner")
def make_main_owner(request: Request, session: DbSession, login: str) -> Response:
    try:
        team.transfer_main_owner(session, auth.actor_for(request, session), login)
    except TeamError as error:
        return _refused(request, error)
    return _back("main-owner", login)


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
