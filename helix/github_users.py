"""Look a GitHub login up when inviting (future_plans.md "Team and services").

Best effort by design: only a definite "no such user" is an answer; a network
error, rate limit or odd reply is None and the invite goes ahead unchecked.
Unauthenticated, so nothing secret is sent.
"""

from dataclasses import dataclass

import httpx

from helix.logging import get_logger

API_ROOT = "https://api.github.com"


@dataclass(frozen=True)
class Profile:
    #: The login in GitHub's own casing.
    login: str
    #: The public profile email, when the person set one.
    email: str | None = None


def _http() -> httpx.Client:
    return httpx.Client(timeout=5.0)


def lookup(login: str) -> Profile | None | bool:
    """The profile; False when GitHub says there is no such user; None when
    GitHub could not be asked or did not make sense."""
    try:
        with _http() as client:
            response = client.get(
                f"{API_ROOT}/users/{login}", headers={"Accept": "application/vnd.github+json"}
            )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        body = response.json()
        found = str(body["login"])
        email = body.get("email")
        return Profile(found, email.strip() if isinstance(email, str) and email.strip() else None)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
        get_logger().warning("github_user_lookup_failed", error=type(exc).__name__)
        return None
