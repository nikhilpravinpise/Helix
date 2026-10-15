"""GitHub token reads for the Connections page's pull request bot card.

Small functions over the REST endpoints the card needs when it works from a
personal token (HELIX_GITHUB_TOKEN) rather than the GitHub App installation
(integrations/github/app.py): the repos the token can push to, and whether a
branch exists. Every failure (transport, HTTP, non-JSON) is a
GitHubOAuthError whose message is a short code (`http_401`) safe to show.
`_http` is the injection seam for tests (an httpx.Client on a MockTransport).
"""

from typing import Any
from urllib.parse import quote

import httpx

API_ROOT = "https://api.github.com"
#: Repo-list pages of 100 followed at most this many times.
MAX_REPO_PAGES = 5


class GitHubOAuthError(Exception):
    """A GitHub REST call failed; the message is a short safe code."""


def _http() -> httpx.Client:
    return httpx.Client(timeout=10.0)


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


def list_pushable_repos(token: str) -> list[str]:
    """The repos the token can push to, as `owner/name`: GET /user/repos with
    owner, collaborator and organization access, keeping those with push
    permission. Paginated, bounded at MAX_REPO_PAGES pages."""
    found: list[str] = []
    for page in range(1, MAX_REPO_PAGES + 1):
        body = _call(
            "GET",
            f"{API_ROOT}/user/repos",
            params={
                "per_page": 100,
                "affiliation": "owner,collaborator,organization_member",
                "sort": "full_name",
                "page": page,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        if not isinstance(body, list):
            raise GitHubOAuthError("malformed_response")
        found += [
            r["full_name"]
            for r in body
            if isinstance(r, dict)
            and r.get("full_name")
            and (r.get("permissions") or {}).get("push")
        ]
        if len(body) < 100:
            break
    return found


def branch_exists(token: str, repo: str, branch: str) -> bool:
    """GET /repos/{repo}/branches/{branch}: True if it exists, False on 404;
    any other failure is a GitHubOAuthError."""
    try:
        _call(
            "GET",
            f"{API_ROOT}/repos/{repo}/branches/{quote(branch, safe='/')}",
            headers={"Authorization": f"Bearer {token}"},
        )
    except GitHubOAuthError as exc:
        if str(exc) == "http_404":
            return False
        raise
    return True
