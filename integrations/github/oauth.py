"""GitHub OAuth for the Connections page (future_plans.md Stage 1).

Connect reuses the Sign in with GitHub OAuth App (helix/auth.py). Small
functions over the endpoints the connect needs, so the UI routes stay thin.
Every failure (transport, HTTP, non-JSON, an `error` reply - GitHub sends
those as HTTP 200) is a GitHubOAuthError whose message is a short code
(`bad_verification_code`, `http_401`) safe to show. `_http` is the injection
seam for tests (an httpx.Client on a MockTransport).
"""

from typing import Any
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API_ROOT = "https://api.github.com"
# Classic OAuth apps have no narrower write scope: `repo` is the least that
# can push a branch and open a PR on a private repo. (The sign-in itself only
# asks read:user.)
SCOPE = "repo"


class GitHubOAuthError(Exception):
    """A GitHub OAuth call failed; the message is a short safe code."""


def _http() -> httpx.Client:
    return httpx.Client(timeout=10.0)


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {"client_id": client_id, "scope": SCOPE, "redirect_uri": redirect_uri, "state": state}
    )
    return f"{AUTHORIZE_URL}?{query}"


def _call(method: str, url: str, **kwargs: Any) -> Any:
    kwargs.setdefault("headers", {})["Accept"] = "application/json"
    try:
        with _http() as client:
            response = client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise GitHubOAuthError("transport_error") from exc
    if response.status_code >= 400:
        raise GitHubOAuthError(f"http_{response.status_code}")
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubOAuthError("non_json_response") from exc


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """The token endpoint: access token and granted scope."""
    body = _call(
        "POST",
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )
    if not isinstance(body, dict):
        raise GitHubOAuthError("malformed_response")
    error = body.get("error")
    if error:
        raise GitHubOAuthError(str(error))
    return body


def login(token: str) -> str:
    """GET /user: the login the token belongs to; also the Test button."""
    body = _call("GET", f"{API_ROOT}/user", headers={"Authorization": f"Bearer {token}"})
    if not isinstance(body, dict) or not body.get("login"):
        raise GitHubOAuthError("malformed_response")
    return str(body["login"])


def revoke(client_id: str, client_secret: str, token: str) -> None:
    """Delete the app's grant for this user, best effort: a failure changes
    nothing for the caller."""
    try:
        _call(
            "DELETE",
            f"{API_ROOT}/applications/{client_id}/grant",
            auth=(client_id, client_secret),
            json={"access_token": token},
        )
    except GitHubOAuthError:
        pass
