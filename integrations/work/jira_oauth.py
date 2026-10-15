"""Atlassian OAuth 2.0 (3LO) for the Connections page (future_plans.md Stage 1).

Small functions over the endpoints the connect needs, so the UI routes stay
thin. Every failure (transport, HTTP, non-JSON, an `error` reply) is a
JiraOAuthError whose message is a short code (`invalid_grant`,
`transport_error`) safe to show. `_http` is the injection seam for tests (an
httpx.Client on a MockTransport), also used for the Test button's Jira call.
"""

from typing import Any
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
ME_URL = "https://api.atlassian.com/me"
# offline_access yields the refresh token; read:me names the account. The
# Jira scopes are what JiraProvider calls: read:jira-work (get/search issues,
# transitions), write:jira-work (create, edit, assign, transition, comment,
# link) and read:jira-user (user search for assignees, /myself for Test).
SCOPES = "offline_access read:me read:jira-work write:jira-work read:jira-user"


class JiraOAuthError(Exception):
    """An Atlassian OAuth call failed; the message is a short safe code."""


def _http() -> httpx.Client:
    return httpx.Client(timeout=10.0)


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "audience": "api.atlassian.com",
            "client_id": client_id,
            "scope": SCOPES,
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _call(method: str, url: str, *, json: dict | None = None, token: str | None = None) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with _http() as client:
            response = client.request(method, url, json=json, headers=headers)
    except httpx.HTTPError as exc:
        raise JiraOAuthError("transport_error") from exc
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code >= 400:
        error = body.get("error") if isinstance(body, dict) else None
        raise JiraOAuthError(
            error if isinstance(error, str) and error else f"http_{response.status_code}"
        )
    if body is None:
        raise JiraOAuthError("non_json_response")
    return body


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """The token endpoint: access and refresh tokens, expiry and granted scope."""
    body = _call(
        "POST",
        TOKEN_URL,
        json={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )
    if not isinstance(body, dict):
        raise JiraOAuthError("malformed_response")
    return body


def accessible_resources(token: str) -> list[dict]:
    """The Atlassian sites this token can reach: {id, url, name, scopes}."""
    body = _call("GET", RESOURCES_URL, token=token)
    if not isinstance(body, list):
        raise JiraOAuthError("malformed_response")
    return [site for site in body if isinstance(site, dict)]


def account_name(token: str) -> str | None:
    """The signed-in person's name or email, or None when it cannot be read."""
    try:
        body = _call("GET", ME_URL, token=token)
    except JiraOAuthError:
        return None
    if not isinstance(body, dict):
        return None
    return body.get("name") or body.get("email") or None
