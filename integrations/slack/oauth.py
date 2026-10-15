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
# to public channels it has not joined. users:read and users:read.email let
# Helix find a teammate's Slack member id from their email (users.lookupByEmail
# needs both), so it can @-mention them and DM their invite.
SCOPES = "chat:write,chat:write.public,channels:read,users:read,users:read.email"


def _http() -> httpx.Client:
    return httpx.Client(timeout=10.0)


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {"client_id": client_id, "scope": SCOPES, "redirect_uri": redirect_uri, "state": state}
    )
    return f"{AUTHORIZE_URL}?{query}"


def _call(
    method: str,
    *,
    data: dict[str, str] | None = None,
    token: str | None = None,
    timeout: float | None = None,
) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with _http() as client:
            response = client.post(
                f"{API_ROOT}/{method}",
                data=data or {},
                headers=headers,
                **({"timeout": timeout} if timeout else {}),
            )
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


def list_channels(token: str, *, limit: int = 1000, timeout: float = 5.0) -> list[dict[str, str]]:
    """conversations.list: the workspace's public, non-archived channels as
    {"id", "name"}, following the cursor up to `limit` channels. A bot token
    without channels:read fails with `missing_scope`."""
    channels: list[dict[str, str]] = []
    cursor = ""
    while len(channels) < limit:
        data = {"types": "public_channel", "exclude_archived": "true", "limit": "200"}
        if cursor:
            data["cursor"] = cursor
        body = _call("conversations.list", data=data, token=token, timeout=timeout)
        for channel in body.get("channels") or []:
            if channel.get("id") and channel.get("name"):
                channels.append({"id": str(channel["id"]), "name": str(channel["name"])})
        cursor = str((body.get("response_metadata") or {}).get("next_cursor") or "")
        if not cursor:
            break
    return channels[:limit]


def lookup_user_by_email(token: str, email: str) -> str | None:
    """users.lookupByEmail: the Slack member id for `email`, or None when the
    workspace has nobody with it. A token without users:read.email fails with
    `missing_scope` (the connection needs reconnecting)."""
    try:
        body = _call("users.lookupByEmail", data={"email": email}, token=token, timeout=5.0)
    except SlackError as exc:
        if str(exc) == "users_not_found":
            return None
        raise
    return str((body.get("user") or {}).get("id") or "") or None


def send_dm(token: str, user_id: str, text: str) -> None:
    """chat.postMessage to a member id, which opens the bot's DM with them."""
    _call("chat.postMessage", data={"channel": user_id, "text": text}, token=token, timeout=5.0)
