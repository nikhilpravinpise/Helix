"""Jenkins evidence capability over the Jenkins REST API.

Auth pattern matches scripts/capture-ci-fixture.sh: HTTP basic auth with
user + API token, plus a crumb (fetched lazily with a session cookie) for
any future POST - this subphase is read-only, so the crumb exists only as
the `_crumb()` helper the 1.8 write path will use.

Read surface:
- build status: `/job/<job>/<n>/api/json` plus `/wfapi/describe.json` for
  pipeline stages (absent plugin -> stages stay empty)
- console log: `/job/<job>/<n>/consoleText` split into lines
- test results: `/job/<job>/<n>/testReport/api/json`

Error mapping is the uniform one: 401/403 -> auth, 404 -> not found,
429 -> rate limit, other >=400 -> EvidenceError, timeout -> timeout,
transport -> EvidenceError, structurally incomplete body -> partial.
"""

import re

import httpx

from helix.config import get_settings
from integrations.evidence import (
    Evidence,
    EvidenceError,
    EvidenceKind,
    EvidenceNotFoundError,
    EvidenceOrigin,
    EvidencePartialError,
    EvidenceSource,
    EvidenceTimeoutError,
    build_evidence,
)
from integrations.http import raise_for_status

_STAGE_RE = re.compile(r"^\[Pipeline\] \{ \((.+)\)\s*$")


class JenkinsEvidenceClient:
    """Live Jenkins evidence capability."""

    def __init__(
        self,
        base_url: str | None = None,
        user: str | None = None,
        token: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.jenkins_url).rstrip("/")
        self._auth = (user or settings.jenkins_user, token or settings.jenkins_token)
        self._client = client or httpx.Client(auth=self._auth, timeout=timeout)

    def _get(self, path: str) -> httpx.Response:
        try:
            response = self._client.get(f"{self._base_url}{path}", auth=self._auth)
        except httpx.TimeoutException as exc:
            raise EvidenceTimeoutError(f"jenkins timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise EvidenceError(f"jenkins transport error: {exc}") from exc
        raise_for_status(response)
        return response

    def _get_json(self, path: str) -> dict:
        try:
            return self._get(path).json()
        except ValueError as exc:
            raise EvidencePartialError(f"jenkins returned non-JSON for {path}") from exc

    def _crumb(self) -> dict[str, str]:
        """CSRF crumb for write calls (the capture-ci-fixture.sh pattern:
        fetch with basic auth, keep the session cookie, send
        <field>: <value> on the POST). Unused by this read-only phase."""
        data = self._get_json("/crumbIssuer/api/json")
        return {data["crumbRequestField"]: data["crumb"]}

    def get_build_status(self, job: str, build_number: int) -> Evidence:
        info = self._get_json(f"/job/{job}/{build_number}/api/json")
        if "number" not in info or ("result" not in info and "building" not in info):
            raise EvidencePartialError(
                f"jenkins build {job}#{build_number} response lacked number/result"
            )
        stages = self._get_stages(job, build_number)
        result = info.get("result") or ("BUILDING" if info.get("building") else None)
        return build_evidence(
            source=EvidenceSource.JENKINS,
            kind=EvidenceKind.BUILD_CONSOLE,
            service=job,
            summary=f"build {job}#{build_number}: {result} ({len(stages)} stage(s))",
            payload={
                "job": job,
                "number": info["number"],
                "result": result,
                "building": info.get("building", False),
                "url": info.get("url"),
                "timestamp": info.get("timestamp"),
                "duration_ms": info.get("duration"),
                "stages": stages,
            },
            origin=EvidenceOrigin.LIVE,
        )

    def _get_stages(self, job: str, build_number: int) -> list[dict]:
        try:
            response = self._get(f"/job/{job}/{build_number}/wfapi/describe.json")
        except EvidenceNotFoundError:
            return []  # pipeline plugin absent or build predates it
        stages = response.json().get("stages") or []
        return [
            {
                "name": s.get("name"),
                "status": s.get("status"),
                "duration_ms": s.get("durationMillis"),
            }
            for s in stages
        ]

    def get_console_log(self, job: str, build_number: int) -> Evidence:
        text = self._get(f"/job/{job}/{build_number}/consoleText").text
        lines = text.splitlines()
        return build_evidence(
            source=EvidenceSource.JENKINS,
            kind=EvidenceKind.BUILD_CONSOLE,
            service=job,
            summary=f"console log for {job}#{build_number}: {len(lines)} line(s)",
            payload={"job": job, "number": build_number, "lines": lines},
            origin=EvidenceOrigin.LIVE,
            total=len(lines),
        )

    def get_test_results(self, job: str, build_number: int) -> Evidence:
        report = self._get_json(f"/job/{job}/{build_number}/testReport/api/json")
        if "totalCount" not in report and "suites" not in report:
            raise EvidencePartialError(
                f"jenkins test report for {job}#{build_number} lacked counts and suites"
            )
        cases = [case for suite in report.get("suites") or [] for case in suite.get("cases") or []]
        failures = [
            {
                "name": c.get("name"),
                "class_name": c.get("className"),
                "status": c.get("status"),
                "message": c.get("errorDetails") or c.get("errorStackTrace"),
            }
            for c in cases
            if c.get("status") in ("FAILED", "REGRESSION")
        ]
        total = report.get("totalCount", len(cases))
        failed = report.get("failCount", len(failures))
        skipped = report.get("skipCount", 0)
        return build_evidence(
            source=EvidenceSource.JENKINS,
            kind=EvidenceKind.TEST_RESULTS,
            service=job,
            summary=(
                f"tests {job}#{build_number}: {total} total, {failed} failed, {skipped} skipped"
            ),
            payload={
                "job": job,
                "number": build_number,
                "total": total,
                "passed": total - failed - skipped,
                "failed": failed,
                "skipped": skipped,
                "failures": failures,
            },
            origin=EvidenceOrigin.LIVE,
            total=total,
        )


def stage_names_from_console(console_text: str) -> list[str]:
    """Stage names parsed out of `[Pipeline] { (Name)` markers - how the
    fixture impl recovers stage info, since CI fixtures carry only the
    console log."""
    return [m.group(1) for line in console_text.splitlines() if (m := _STAGE_RE.match(line))]
