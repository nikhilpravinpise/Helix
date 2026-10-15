"""Connections page (future_plans.md Stage 1): connect, test and disconnect
the accounts Helix talks to, without touching .env.

    GET  /ui/connections                      one card per provider
    POST /ui/connections/slack/connect        redirect to Slack's OAuth install
    GET  /ui/connections/slack/callback       verify state, swap the code, save
    POST /ui/connections/slack/disconnect     revoke (best effort) and delete
    POST /ui/connections/slack/test           auth.test with the stored token

The OAuth `state` is a signed cookie like the GitHub sign-in's (helix/auth.py),
extended with the signed-in user so a callback started by someone else is
refused. Browsers never see a token: the page shows status and account only.
POSTs are protected the way the rest of the dashboard is, by the SameSite=Lax
session cookie. Adding a provider is one entry in `_providers` plus its routes.
"""

import hmac
import re
import secrets
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from helix import auth
from helix.config import get_settings
from helix.ui import DbSession, _incident_total, _pending_count, templates
from integrations.slack import SlackError, oauth
from state import connections

STATE_COOKIE = "helix_connect_state"
STATE_SECONDS = 600
_DETAIL = re.compile(r"[a-z0-9_]{1,64}")

router = APIRouter(include_in_schema=False)

# notice code -> (tone, text); {detail} is a Slack error code.
_NOTICES = {
    "slack-connected": ("ok", "Connected Slack."),
    "slack-denied": ("bad", "Slack connection cancelled - nothing was saved."),
    "slack-failed": ("bad", "Slack did not complete the connection ({detail}). Nothing was saved."),
    "slack-unconfigured": ("bad", "Slack cannot be connected until the server is configured."),
    "slack-key-missing": ("bad", "HELIX_CONNECTIONS_KEY is not set, so nothing was saved."),
    "slack-disconnected": ("ok", "Disconnected Slack."),
    "slack-test-ok": ("ok", "Slack accepted the connection."),
    "slack-test-failed": ("bad", "Slack rejected the connection ({detail})."),
    "slack-not-connected": ("bad", "Slack is not connected."),
}


def _slack_missing() -> str | None:
    """Why Connect Slack cannot start, or None when it can."""
    settings = get_settings()
    for value, name in (
        (settings.connections_key, "HELIX_CONNECTIONS_KEY"),
        (settings.slack_client_id, "HELIX_SLACK_CLIENT_ID"),
        (settings.slack_client_secret, "HELIX_SLACK_CLIENT_SECRET"),
        (settings.session_secret, "HELIX_SESSION_SECRET"),
    ):
        if not value:
            return f"Set {name} on the server to enable Connect."
    return None


def _providers(session) -> list[dict[str, Any]]:
    """One entry per provider card, in display order."""
    settings = get_settings()
    slack = connections.get_connection(session, "slack")
    missing = _slack_missing()
    return [
        {
            "id": "slack",
            "name": "Slack",
            "blurb": "Incident threads and approval buttons post to the connected workspace.",
            "connected": slack is not None,
            "account": slack.account if slack else None,
            "scopes": slack.scopes if slack else [],
            "channel": slack.settings.get("channel") if slack else None,
            "uses_server": slack is None and bool(settings.slack_bot_token),
            "can_connect": missing is None,
            "reason": missing,
        }
    ]


def _render(request: Request, session, *, notice: tuple[str, str] | None, status_code: int = 200):
    return templates.TemplateResponse(
        request,
        "connections.html",
        {
            "active": "connections",
            "providers": _providers(session),
            "notice": notice,
            "incident_total": _incident_total(session),
            "pending_count": _pending_count(session),
        },
        status_code=status_code,
    )


def _back(code: str, detail: str = "") -> RedirectResponse:
    suffix = f"&detail={detail}" if _DETAIL.fullmatch(detail) else ""
    return RedirectResponse(f"/ui/connections?notice={code}{suffix}", status_code=303)


def _notice(code: str, detail: str) -> tuple[str, str] | None:
    if code not in _NOTICES:
        return None
    tone, text = _NOTICES[code]
    return tone, text.format(detail=detail if _DETAIL.fullmatch(detail) else "unknown_error")


@router.get("/ui/connections", response_class=HTMLResponse)
def connections_page(
    request: Request, session: DbSession, notice: str = "", detail: str = ""
) -> Response:
    return _render(request, session, notice=_notice(notice, detail))


def _user(request: Request) -> str | None:
    return getattr(request.state, "user", None)


@router.post("/ui/connections/slack/connect")
def slack_connect(request: Request) -> Response:
    if _slack_missing():
        return _back("slack-unconfigured")
    state = secrets.token_urlsafe(24)
    response = RedirectResponse(
        oauth.authorize_url(
            get_settings().slack_client_id or "",
            f"{auth._public_url(request)}/ui/connections/slack/callback",
            state,
        ),
        status_code=303,
    )
    response.set_cookie(
        STATE_COOKIE,
        auth._sign(
            {
                "state": state,
                "provider": "slack",
                "user": _user(request),
                "exp": time.time() + STATE_SECONDS,
            }
        ),
        max_age=STATE_SECONDS,
        httponly=True,
        secure=auth._secure(request),
        samesite="lax",
    )
    return response


def _state_ok(request: Request, state: str) -> bool:
    if not get_settings().session_secret:
        return False
    bound = auth._unsign(request.cookies.get(STATE_COOKIE)) or {}
    expected = bound.get("state")
    return bool(
        state
        and expected
        and bound.get("provider") == "slack"
        and bound.get("user") == _user(request)
        and hmac.compare_digest(state, expected)
    )


@router.get("/ui/connections/slack/callback")
def slack_callback(
    request: Request, session: DbSession, code: str = "", state: str = "", error: str = ""
) -> Response:
    if not _state_ok(request, state):
        return _render(
            request,
            session,
            notice=(
                "bad",
                "That Slack connection did not start from this page, so it was refused.",
            ),
            status_code=400,
        )
    if error or not code:
        response = _back("slack-denied" if error == "access_denied" else "slack-failed", error)
    elif _slack_missing():
        response = _back("slack-unconfigured")
    else:
        response = _finish_slack(request, session, code)
    response.delete_cookie(STATE_COOKIE)
    return response


def _finish_slack(request: Request, session, code: str) -> RedirectResponse:
    settings = get_settings()
    try:
        granted = oauth.exchange_code(
            settings.slack_client_id or "",
            settings.slack_client_secret or "",
            code,
            f"{auth._public_url(request)}/ui/connections/slack/callback",
        )
        team = granted["team"]
        token = granted["access_token"]
    except SlackError as exc:
        return _back("slack-failed", str(exc))
    except (KeyError, TypeError):
        return _back("slack-failed", "malformed_response")
    kept = connections.get_connection(session, "slack")
    channel = (
        kept.settings.get("channel")
        if kept and kept.settings.get("team_id") == team.get("id")
        else None
    )
    extra = {"channel": channel} if channel else {}
    try:
        connections.save_connection(
            session,
            "slack",
            account=team.get("name") or team.get("id") or "Slack",
            scopes=[s for s in str(granted.get("scope", "")).split(",") if s],
            access_token=token,
            refresh_token=None,
            expires_at=None,
            settings={
                "team_id": team.get("id"),
                "team": team.get("name"),
                "bot_user_id": granted.get("bot_user_id"),
                **extra,
            },
        )
    except connections.ConnectionsKeyMissing:
        return _back("slack-key-missing")
    session.commit()
    return _back("slack-connected")


@router.post("/ui/connections/slack/disconnect")
def slack_disconnect(session: DbSession) -> Response:
    stored = connections.get_connection(session, "slack")
    if stored is not None:
        oauth.revoke(stored.access_token)
    connections.delete_connection(session, "slack")
    session.commit()
    return _back("slack-disconnected")


@router.post("/ui/connections/slack/test")
def slack_test(session: DbSession) -> Response:
    stored = connections.get_connection(session, "slack")
    if stored is None:
        return _back("slack-not-connected")
    try:
        oauth.auth_test(stored.access_token)
    except SlackError as exc:
        return _back("slack-test-failed", str(exc))
    return _back("slack-test-ok")
