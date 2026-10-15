"""Sign in with GitHub for a hosted Helix (decided by Vraj 2026-10-14).

Active only when a GitHub OAuth app is configured
(HELIX_GITHUB_OAUTH_CLIENT_ID / _SECRET). Then every page needs a signed-in
GitHub user on the allow-list (HELIX_ALLOWED_USERS, default: the
people table, plus anyone with a pending team invite, who is let in and
accepted by signing in): pages send a signed-out visitor to /login, API
routes answer 401. Machine clients keep using the intake token as a Bearer
header. /healthz, /slack/interactions (Slack-signed) and the sign-in routes
stay open, and so do "/", the public landing page (a signed-in visitor
is sent on to the dashboard), and /invite/{token}, the page an invite link
opens.

Managing the team and the connections needs an admin (`require_admin`).

The session is a cookie holding the GitHub login and an expiry, signed with
HMAC-SHA256 under HELIX_SESSION_SECRET; no passwords exist anywhere. The
OAuth `state` is a second signed cookie, checked on the callback, so a
callback Helix did not start is refused.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from helix import slack_people
from helix.config import get_settings
from helix.logging import get_logger
from helix.ui import DbSession, templates
from state import models, team
from state.team import TeamError

SESSION_COOKIE = "helix_session"
STATE_COOKIE = "helix_oauth_state"
SESSION_SECONDS = 12 * 3600
_OPEN_PATHS = frozenset(
    {
        "/",
        "/healthz",
        "/slack/interactions",
        "/login",
        "/logout",
        "/auth/github",
        "/auth/github/callback",
    }
)

LINK_SLACK_PATH = "/ui/team/link-slack"

router = APIRouter()


def _is_invite_link(path: str) -> bool:
    """/invite/{token} - a single non-empty segment, nothing deeper."""
    token = path.removeprefix("/invite/")
    return path.startswith("/invite/") and bool(token) and "/" not in token


def enabled() -> bool:
    return bool(get_settings().github_oauth_client_id)


def allowed_users() -> set[str]:
    """HELIX_ALLOWED_USERS when set, else everyone in the people table."""
    configured = get_settings().allowed_users
    if configured:
        return {name.strip() for name in configured.split(",") if name.strip()}
    from agent.ops.registry import load_registry

    return set(load_registry().people)


def _secret() -> bytes:
    secret = get_settings().session_secret
    if not secret:
        raise RuntimeError("HELIX_SESSION_SECRET must be set when GitHub sign-in is enabled")
    return secret.encode()


def _sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    mac = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def _unsign(value: str | None) -> dict | None:
    if not value or "." not in value:
        return None
    body, _, mac = value.rpartition(".")
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body))
    except ValueError:
        return None
    return payload if payload.get("exp", 0) > time.time() else None


def signed_in_user(request: Request) -> str | None:
    """The GitHub login of the signed-in user, or None."""
    payload = _unsign(request.cookies.get(SESSION_COOKIE))
    login = (payload or {}).get("login")
    return login if login in allowed_users() else None


def shows_landing(request: Request) -> bool:
    """True when "/" should render the public landing page: sign-in is on
    and the visitor has no valid session. Otherwise "/" goes to the dashboard."""
    return enabled() and signed_in_user(request) is None


def _public_url(request: Request) -> str:
    return (get_settings().public_url or str(request.base_url)).rstrip("/")


def _secure(request: Request) -> bool:
    return _public_url(request).startswith("https://")


@dataclass(frozen=True)
class GitHubUser:
    """The signed-in user's GitHub login, profile name (None when the profile
    has none) and verified email addresses (empty when GitHub would not say)."""

    login: str
    name: str | None = None
    verified_emails: tuple[str, ...] = ()


def github_login(code: str, redirect_uri: str) -> GitHubUser:
    """Exchange the OAuth code for the user's GitHub login, profile name and
    verified emails. The emails are best effort: they only help find the
    person in Slack, so a failure there never blocks sign-in."""
    settings = get_settings()
    token = httpx.post(
        "https://github.com/login/oauth/access_token",
        data={
            "client_id": settings.github_oauth_client_id,
            "client_secret": settings.github_oauth_client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"Accept": "application/json"},
        timeout=10.0,
    ).json()["access_token"]
    user = httpx.get(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=10.0,
    )
    user.raise_for_status()
    profile = user.json()
    return GitHubUser(str(profile["login"]), profile.get("name"), _verified_emails(token))


def _verified_emails(token: str) -> tuple[str, ...]:
    try:
        response = httpx.get(
            "https://api.github.com/user/emails",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=10.0,
        )
        response.raise_for_status()
        return tuple(
            str(row["email"]) for row in response.json() if row.get("verified") and row.get("email")
        )
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError) as exc:
        get_logger().warning("github_emails_failed", error=type(exc).__name__)
        return ()


def safe_next(value: str | None) -> str | None:
    """Where sign-in may send the visitor afterwards: a relative path under
    /ui/ and nothing else, so a crafted link cannot bounce them to another site."""
    if not value or not value.startswith("/ui/") or "//" in value or "\\" in value:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or ".." in parts.path.split("/"):
        return None
    return value


def _sign_in_query(next_url: str | None) -> str:
    return f"?{urlencode({'next': next_url})}" if next_url else ""


def gate(request: Request) -> Response | None:
    """None when the request may proceed; otherwise the response to send."""
    path = request.url.path
    if not enabled() or path in _OPEN_PATHS or path.startswith("/static/") or _is_invite_link(path):
        return None
    token = get_settings().intake_token
    scheme, _, credential = (request.headers.get("authorization") or "").partition(" ")
    if token and scheme.lower() == "bearer" and hmac.compare_digest(credential, token):
        return None
    user = signed_in_user(request)
    if user:
        request.state.user = user
        return None
    if path == LINK_SLACK_PATH and request.method == "GET":
        # Come back to the link after signing in.
        target = f"{path}?{request.url.query}" if request.url.query else path
        return RedirectResponse(f"/login{_sign_in_query(safe_next(target))}", status_code=303)
    if request.url.path.startswith("/ui"):
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": "sign in required"}, status_code=401)


def _auth_page(request: Request, state: str, status_code: int = 200, login: str = "") -> Response:
    return templates.TemplateResponse(
        request, "login.html", {"state": state, "login": login}, status_code=status_code
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "") -> Response:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"state": "login", "login": "", "sign_in_query": _sign_in_query(safe_next(next))},
    )


@router.get("/auth/github")
def start(request: Request, next: str = "") -> Response:
    state = secrets.token_urlsafe(24)
    query = urlencode(
        {
            "client_id": get_settings().github_oauth_client_id,
            "redirect_uri": f"{_public_url(request)}/auth/github/callback",
            "scope": "read:user user:email",
            "state": state,
            "allow_signup": "false",
        }
    )
    response = RedirectResponse(f"https://github.com/login/oauth/authorize?{query}", 303)
    response.set_cookie(
        STATE_COOKIE,
        _sign({"state": state, "next": safe_next(next), "exp": time.time() + 600}),
        max_age=600,
        httponly=True,
        secure=_secure(request),
        samesite="lax",
    )
    return response


@router.get("/auth/github/callback")
def callback(request: Request, session: DbSession, code: str = "", state: str = "") -> Response:
    pending = _unsign(request.cookies.get(STATE_COOKIE)) or {}
    expected = pending.get("state")
    if not code or not expected or not hmac.compare_digest(state, expected):
        return _auth_page(request, "expired", 400)
    user = github_login(code, f"{_public_url(request)}/auth/github/callback")
    login = user.login
    if login not in allowed_users():
        # A configured HELIX_ALLOWED_USERS is the exact list; otherwise a
        # pending invite for this login (any case) lets them in.
        accepted = None
        if not get_settings().allowed_users:
            accepted = team.accept_on_sign_in(session, login, user.name)
        if accepted is None:
            return _auth_page(request, "denied", 403, login)
        login = accepted.login
    team.record_sign_in(session, login)
    _link_slack_on_sign_in(session, login, user.verified_emails)
    session.commit()
    response = RedirectResponse(safe_next(pending.get("next")) or "/ui/", status_code=303)
    response.delete_cookie(STATE_COOKIE)
    response.set_cookie(
        SESSION_COOKIE,
        _sign({"login": login, "exp": time.time() + SESSION_SECONDS}),
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=_secure(request),
        samesite="lax",
    )
    return response


def _link_slack_on_sign_in(session: Session, login: str, verified: tuple[str, ...]) -> None:
    """Find a person with no Slack id in Slack: their stored email, then each
    verified GitHub email. Never blocks the sign-in."""
    person = session.get(models.Person, login)
    if person is None or person.slack_id:
        return
    emails = list(dict.fromkeys(e for e in (person.email, *verified) if e))
    if not emails:
        return
    found = slack_people.find_by_emails(session, emails)
    if found.slack_id:
        try:
            team.link_slack(session, login, found.slack_id, found.email)
        except TeamError as exc:
            get_logger().warning("slack_link_on_sign_in_refused", reason=str(exc))


def may_manage(request: Request, session: Session) -> bool:
    """Whether the request's user may change the team and the connections
    (people.role is admin). With sign-in off there is no user and the request
    is trusted, as everywhere else. A user the HELIX_ALLOWED_USERS override
    lets in without a people row is trusted too: the operator named them."""
    user = getattr(request.state, "user", None)
    if user is None:
        return not enabled()
    if team.is_admin(session, user):
        return True
    return bool(get_settings().allowed_users) and session.get(models.Person, user) is None


def require_admin(request: Request, session: DbSession) -> None:
    """Dependency for the team and connections routers: a POST needs an admin."""
    if request.method == "POST" and not may_manage(request, session):
        raise HTTPException(403, "Only admins can make this change.")


AdminOnly = Depends(require_admin)


@router.get("/logout")
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
