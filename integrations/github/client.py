"""GitHub evidence capability over the REST API (httpx).

Targets the Helix repository itself (D5) so commits, diffs, PRs, reviews
and check runs are real. Read-only in this subphase; the controlled write
path (helix/INC-* branches, sandbox/payment-api paths only) lands in 1.8.

Error mapping is the uniform one: 401/403 -> auth, 404 -> not found,
429 -> rate limit, other >=400 -> EvidenceError, timeout -> timeout,
transport -> EvidenceError, structurally incomplete body -> partial.
"""

from typing import Any

import httpx

from helix.config import get_settings
from integrations.evidence import (
    Evidence,
    EvidenceError,
    EvidenceKind,
    EvidenceOrigin,
    EvidencePartialError,
    EvidenceSource,
    EvidenceTimeoutError,
    build_evidence,
)
from integrations.http import raise_for_status
from state import connections

API_ROOT = "https://api.github.com"


class GitHubEvidenceClient:
    """Live GitHub evidence capability."""

    def __init__(
        self,
        repo: str | None = None,
        token: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        settings = get_settings()
        self._repo = repo or settings.github_repo
        self._token = token
        if client is not None:
            self._client = client
        else:
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            self._client = httpx.Client(base_url=API_ROOT, headers=headers, timeout=timeout)

    def _resolve_token(self) -> str | None:
        """Explicit token, else the connected GitHub account, else
        HELIX_GITHUB_TOKEN. Resolved on first use and kept, so building the
        client never touches the store."""
        if self._token is None:
            record = connections.load_connection("github")
            self._token = record.access_token if record else get_settings().github_token
        return self._token

    def _get(self, path: str, **params: Any) -> httpx.Response:
        token = self._resolve_token()
        try:
            response = self._client.get(
                path,
                params={k: v for k, v in params.items() if v is not None},
                headers={"Authorization": f"Bearer {token}"} if token else None,
            )
        except httpx.TimeoutException as exc:
            raise EvidenceTimeoutError(f"github timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise EvidenceError(f"github transport error: {exc}") from exc
        raise_for_status(response)
        return response

    def _get_json(self, path: str, **params: Any) -> Any:
        try:
            return self._get(path, **params).json()
        except ValueError as exc:
            raise EvidencePartialError(f"github returned non-JSON for {path}") from exc

    def get_commit(self, sha: str) -> Evidence:
        data = self._get_json(f"/repos/{self._repo}/commits/{sha}")
        if "sha" not in data or "commit" not in data:
            raise EvidencePartialError(f"github commit {sha} response lacked sha/commit")
        commit = data["commit"]
        author = commit.get("author") or {}
        return build_evidence(
            source=EvidenceSource.GITHUB,
            kind=EvidenceKind.COMMIT,
            service=self._repo,
            summary=f"commit {sha[:8]}: {(commit.get('message') or '').splitlines()[0][:80]}",
            payload={
                "sha": data["sha"],
                "message": commit.get("message"),
                "author_name": author.get("name"),
                "author_date": author.get("date"),
                "parents": [p.get("sha") for p in data.get("parents") or []],
                "url": data.get("html_url"),
            },
            origin=EvidenceOrigin.LIVE,
        )

    def get_diff(self, sha: str) -> Evidence:
        data = self._get_json(f"/repos/{self._repo}/commits/{sha}")
        files = [
            {
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "patch": f.get("patch"),
            }
            for f in data.get("files") or []
        ]
        return build_evidence(
            source=EvidenceSource.GITHUB,
            kind=EvidenceKind.DIFF,
            service=self._repo,
            summary=f"diff for {sha[:8]}: {len(files)} file(s) changed",
            payload={"sha": data.get("sha", sha), "files": files},
            origin=EvidenceOrigin.LIVE,
            total=len(files),
        )

    def list_open_pull_requests_raw(self) -> list[dict[str, Any]]:
        """The repository's open pull requests as GitHub returns them - the
        `helix watch --source github` feed, not diagnosis evidence (D18:
        fetching lives here, turning it into events does not)."""
        data = self._get_json(f"/repos/{self._repo}/pulls", state="open", per_page=100)
        if not isinstance(data, list):
            raise EvidencePartialError("github open-PR listing was not a list")
        return data

    def get_pull_request(self, number: int) -> Evidence:
        data = self._get_json(f"/repos/{self._repo}/pulls/{number}")
        if "number" not in data:
            raise EvidencePartialError(f"github PR {number} response lacked number")
        return build_evidence(
            source=EvidenceSource.GITHUB,
            kind=EvidenceKind.PR_INFO,
            service=self._repo,
            summary=f"PR #{number}: {data.get('state')} - {data.get('title')}",
            payload={
                "number": data["number"],
                "title": data.get("title"),
                "state": data.get("state"),
                "merged": data.get("merged"),
                "mergeable": data.get("mergeable"),
                "draft": data.get("draft"),
                "head_sha": (data.get("head") or {}).get("sha"),
                "head_ref": (data.get("head") or {}).get("ref"),
                "base_ref": (data.get("base") or {}).get("ref"),
                "author": (data.get("user") or {}).get("login"),
                "url": data.get("html_url"),
            },
            origin=EvidenceOrigin.LIVE,
        )

    def get_pr_reviews(self, number: int) -> Evidence:
        data = self._get_json(f"/repos/{self._repo}/pulls/{number}/reviews", per_page=100)
        if not isinstance(data, list):
            raise EvidencePartialError(f"github reviews for PR {number} were not a list")
        reviews = [
            {
                "id": r.get("id"),
                "user": (r.get("user") or {}).get("login"),
                "state": r.get("state"),
                "body": r.get("body"),
                "submitted_at": r.get("submitted_at"),
            }
            for r in data
        ]
        return build_evidence(
            source=EvidenceSource.GITHUB,
            kind=EvidenceKind.PR_REVIEW,
            service=self._repo,
            summary=f"{len(reviews)} review(s) on PR #{number}",
            payload={"number": number, "items": reviews},
            origin=EvidenceOrigin.LIVE,
            total=len(reviews),
        )

    def get_check_runs(self, ref: str) -> Evidence:
        data = self._get_json(f"/repos/{self._repo}/commits/{ref}/check-runs", per_page=100)
        if not isinstance(data, dict) or "check_runs" not in data:
            raise EvidencePartialError(f"github check-runs for {ref} lacked check_runs")
        runs = [
            {
                "name": r.get("name"),
                "status": r.get("status"),
                "conclusion": r.get("conclusion"),
                "started_at": r.get("started_at"),
                "completed_at": r.get("completed_at"),
                "url": r.get("html_url"),
            }
            for r in data["check_runs"]
        ]
        total = data.get("total_count", len(runs))
        return build_evidence(
            source=EvidenceSource.GITHUB,
            kind=EvidenceKind.CHECK_RUN,
            service=self._repo,
            summary=f"{len(runs)} check run(s) on {ref}",
            payload={"ref": ref, "items": runs},
            origin=EvidenceOrigin.LIVE,
            truncated=total > len(runs),
            total=total,
        )

    def get_branch_state(self, branch: str, *, base: str | None = None) -> Evidence:
        data = self._get_json(f"/repos/{self._repo}/branches/{branch}")
        if "name" not in data:
            raise EvidencePartialError(f"github branch {branch} response lacked name")
        payload: dict[str, Any] = {
            "name": data["name"],
            "sha": (data.get("commit") or {}).get("sha"),
            "protected": data.get("protected"),
            "base": base,
        }
        summary = f"branch {branch} at {str(payload['sha'])[:8]}"
        if base:
            cmp_data = self._get_json(f"/repos/{self._repo}/compare/{base}...{branch}")
            payload["ahead_by"] = cmp_data.get("ahead_by")
            payload["behind_by"] = cmp_data.get("behind_by")
            payload["comparison"] = cmp_data.get("status")
            summary += f", {payload['ahead_by']} ahead / {payload['behind_by']} behind {base}"
        return build_evidence(
            source=EvidenceSource.GITHUB,
            kind=EvidenceKind.BRANCH_STATE,
            service=self._repo,
            summary=summary,
            payload=payload,
            origin=EvidenceOrigin.LIVE,
        )
