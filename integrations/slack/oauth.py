"""Slack OAuth v2 install for the Connections page (future_plans.md Stage 1).

Small functions over the three Slack endpoints the install needs, so the UI
routes stay thin. Slack answers HTTP 200 with `ok: false` on failure; every
failure here (transport, HTTP, non-JSON, `ok: false`) is a SlackError whose
message is a short code (`invalid_code`, `transport_error`) safe to show. `_http` is
the injection seam for tests (an httpx.Client on a MockTransport).
"""

from typing import Any
from urllib.parse import urlencode

import httpx

from integrations.slack.client import SlackError

AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
API_ROOT = "https://slack.com/api"
# channels:read feeds the channel picker; chat:write.public lets the bot post
# to public channels it has not joined.
SCOPES = "chat:write,chat:write.public,channels:read"


def _http() -> httpx.Client:
    return httpx.Client(timeout=10.0)


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {"client_id": client_id, "scope": SCOPES, "redirect_uri": redirect_uri, "state": state}
    )
    return f"{AUTHORIZE_URL}?{query}"


def _call(method: str, *, data: dict[str, str] | None = None, token: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with _http() as client:
            response = client.post(f"{API_ROOT}/{method}", data=data or {}, headers=headers)
    except httpx.HTTPError as exc:
        raise SlackError("transport_error") from exc
    if response.status_code >= 400:
        raise SlackError(f"http_{response.status_code}")
    try:
        body: dict[str, Any] = response.json()
    except ValueError as exc:
        raise SlackError("non_json_response") from exc
    if not body.get("ok"):
        raise SlackError(str(body.get("error") or "unknown_error"))
    return body


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """oauth.v2.access: the bot token, granted scopes, team and bot user."""
    return _call(
        "oauth.v2.access",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )


def auth_test(token: str) -> dict:
    """auth.test: confirms the stored token still works; names the team."""
    return _call("auth.test", token=token)


def revoke(token: str) -> None:
    """auth.revoke, best effort: a failure changes nothing for the caller."""
    try:
        _call("auth.revoke", token=token)
    except SlackError:
        pass
