"""Controlled GitHub writes (subphase 1.8): the durable-fix path.

Everything goes through the REST API (never local git - checkouts,
credentials and push rights stay server-side): read base ref -> create
the incident branch ref -> PUT file contents onto it -> open a PR. There
is deliberately no merge call anywhere in this module: Helix proposes,
humans merge (C5).

Two bounds are enforced HERE, before any HTTP request is made, so a
policy or planner bug cannot launder a bad write through the client:

- `_check_branch`: only `helix/INC-*` branches may be written. `main`,
  `develop`, release branches - anything else is refused outright, which
  is what makes "no direct writes to a protected branch" structural rather
  than documented.
- `_check_path`: every file path must sit under `sandbox/payment-api/`
  with no traversal. These two rules mirror the catalogue params bounds
  (agent/actions/catalogue.py) on purpose - each layer re-asserts the
  boundary itself.

Error mapping: 401/403 -> WriteAuthError, 404 -> WriteNotFoundError,
429 -> WriteRateLimitError, timeout/transport -> WriteTimeoutError /
WriteError.
"""

import base64
import re
from typing import Any

import httpx

from helix.config import get_settings
from integrations.writes import (
    WriteAuthError,
    WriteError,
    WriteNotFoundError,
    WriteRateLimitError,
    WriteRefusedError,
    WriteTimeoutError,
)
from state import connections

GITHUB_API = "https://api.github.com"

#: Mirrors the catalogue bounds (OpenFixPRParams) - kept local so the
#: client enforces them independently of the params model.
FIX_BRANCH_RE = re.compile(r"^helix/INC-[A-Za-z0-9-]+$")
WRITE_ROOT = "sandbox/payment-api/"
BASE_BRANCH = "main"


def _check_branch(branch: str) -> None:
    """Refuse any branch that is not `helix/INC-*`. This is what makes
    protected branches unwritable through Helix, in code."""
    if not FIX_BRANCH_RE.match(branch or ""):
        raise WriteRefusedError(
            f"refusing to write branch {branch!r}: only helix/INC-* branches are writable"
        )


def _check_path(path: str) -> None:
    """Refuse any file path outside the sandbox/payment-api/ write root."""
    if ".." in path or not path.startswith(WRITE_ROOT):
        raise WriteRefusedError(f"refusing to write path {path!r}: only {WRITE_ROOT}* is writable")


class GitHubWriteClient:
    """REST write capability for the fix-PR action.

    `client` is an injection seam for tests (an httpx.Client with a
    MockTransport); a real client is created lazily on first call so
    importing/constructing never requires network or a token."""

    def __init__(
        self,
        repo: str | None = None,
        token: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        base_branch: str = BASE_BRANCH,
    ) -> None:
        self._repo = repo or get_settings().github_repo
        self._token = token
        self._client = client
        self._timeout = timeout
        self._base_branch = base_branch

    def _resolve_token(self) -> str | None:
        """Explicit token, else the connected GitHub account, else
        HELIX_GITHUB_TOKEN. Resolved on first use and kept, so building the
        client never touches the store and one PR uses one identity."""
        if self._token is None:
            record = connections.load_connection("github")
            self._token = record.access_token if record else get_settings().github_token
        return self._token

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _request(
        self, method: str, path: str, *, json: dict | None = None
    ) -> dict[str, Any] | list[Any] | None:
        token = self._resolve_token()
        if not token:
            raise WriteAuthError("no GitHub connection and HELIX_GITHUB_TOKEN is not set")
        url = f"{GITHUB_API}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = self._http().request(method, url, headers=headers, json=json)
        except httpx.TimeoutException as exc:
            raise WriteTimeoutError(f"github {method} {path} timed out") from exc
        except httpx.HTTPError as exc:
            raise WriteError(f"github transport error on {method} {path}: {exc}") from exc
        status = response.status_code
        if status in (401, 403):
            raise WriteAuthError(f"github {method} {path}: HTTP {status}")
        if status == 404:
            raise WriteNotFoundError(f"github {method} {path}: not found")
        if status == 429:
            raise WriteRateLimitError(f"github {method} {path}: rate limited")
        if status >= 400:
            raise WriteError(f"github {method} {path}: HTTP {status} - {response.text[:300]}")
        if status == 204 or not response.content:
            return None
        return response.json()

    def _contents_sha(self, path: str, branch: str) -> str | None:
        """The existing file's blob sha on `branch` (required by the
        contents API for updates), or None when the file does not exist."""
        try:
            data = self._request("GET", f"/repos/{self._repo}/contents/{path}?ref={branch}")
        except WriteNotFoundError:
            return None
        return (data or {}).get("sha")

    def open_fix_pr(
        self,
        *,
        branch: str,
        files: list[dict[str, str]],
        title: str,
        body: str = "",
    ) -> dict[str, Any]:
        """Create `helix/INC-*` from the base branch, commit each file via
        the contents API, then open a PR. Refusals fire before any HTTP
        call; there is no merge - the PR awaits human review."""
        _check_branch(branch)
        for change in files:
            _check_path(change["path"])

        base = self._request("GET", f"/repos/{self._repo}/git/ref/heads/{self._base_branch}")
        base_sha = (base or {}).get("object", {}).get("sha")
        if not base_sha:
            raise WriteError(f"github base ref heads/{self._base_branch} carried no sha")

        try:
            self._request(
                "POST",
                f"/repos/{self._repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            )
        except WriteError as exc:
            # A resumed/retried run can hit an already-created branch -
            # reuse it rather than failing the incident.
            if "Reference already exists" not in str(exc):
                raise

        for change in files:
            path = change["path"]
            payload: dict[str, Any] = {
                "message": f"helix: update {path}",
                "content": base64.b64encode(change["content"].encode()).decode(),
                "branch": branch,
            }
            existing = self._contents_sha(path, branch)
            if existing:
                payload["sha"] = existing
            self._request("PUT", f"/repos/{self._repo}/contents/{path}", json=payload)

        pr = self._request(
            "POST",
            f"/repos/{self._repo}/pulls",
            json={
                "title": title,
                "head": branch,
                "base": self._base_branch,
                "body": body,
            },
        )
        pr = pr or {}
        return {
            "op": "open_fix_pr",
            "repo": self._repo,
            "branch": branch,
            "base": self._base_branch,
            "pr_number": pr.get("number"),
            "pr_url": pr.get("html_url"),
            "files": [c["path"] for c in files],
            "origin": "live",
        }

    def close_pr(self, *, branch: str, pr_number: int | None = None) -> dict[str, Any]:
        """Close the open PR on `branch` - the executor-side compensation
        for open_fix_pr (subphase 1.10): the branch stays (it is the
        audit trail of what was proposed) but the PR is withdrawn.

        Resolves the PR by `pr_number` when the failed execution recorded
        one, else by head-branch lookup. Idempotent: no open PR on the
        branch (already closed, or the open call never landed) reports
        success instead of failing - a retried compensation must not
        error on work already done. There is still no merge - closing is
        the inverse of opening, not of merging."""
        _check_branch(branch)
        number = pr_number
        if number is None:
            owner = self._repo.split("/", 1)[0]
            pulls = (
                self._request(
                    "GET",
                    f"/repos/{self._repo}/pulls?head={owner}:{branch}&state=open",
                )
                or []
            )
            if not pulls:
                return {
                    "op": "close_pr",
                    "repo": self._repo,
                    "branch": branch,
                    "pr_number": None,
                    "state": "closed",
                    "detail": "no open PR on the branch - nothing to undo",
                    "origin": "live",
                }
            number = pulls[0].get("number")
        pr = (
            self._request(
                "PATCH",
                f"/repos/{self._repo}/pulls/{number}",
                json={"state": "closed"},
            )
            or {}
        )
        return {
            "op": "close_pr",
            "repo": self._repo,
            "branch": branch,
            "pr_number": pr.get("number", number),
            "state": pr.get("state", "closed"),
            "origin": "live",
        }
