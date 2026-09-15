"""Controlled Jenkins writes (subphase 1.8): one verb - trigger a job
build (the `rerun_pipeline` action).

Auth mirrors the evidence client and scripts/capture-ci-fixture.sh:
HTTP basic auth with user + API token, a CSRF crumb fetched from
/crumbIssuer/api/json and sent as `<field>: <value>` on the POST, and the
session cookie kept by the shared httpx client. Jenkins without the crumb
issuer plugin returns 404 there - treated as "no crumb required" so the
client still works against crumb-free setups.

`POST /job/<job>/build` answers 201 with a `Location` header pointing at
the queued item; that URL is the audit-worthy result. Error mapping is
the uniform write one: 401/403 -> auth, 404 -> not found, 429 -> rate
limit, timeout -> timeout, transport/other -> WriteError.
"""

from typing import Any

import httpx

from helix.config import get_settings
from integrations.writes import (
    WriteAuthError,
    WriteError,
    WriteNotFoundError,
    WriteRateLimitError,
    WriteTimeoutError,
)


class JenkinsWriteClient:
    """Jenkins build-trigger capability.

    `client` is the injection seam for tests (MockTransport); a real
    client is created lazily on first call."""

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
        self._client = client
        self._timeout = timeout

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(auth=self._auth, timeout=self._timeout)
        return self._client

    def _request(self, method: str, path: str, *, headers: dict[str, str] | None = None) -> Any:
        try:
            response = self._http().request(
                method, f"{self._base_url}{path}", auth=self._auth, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise WriteTimeoutError(f"jenkins {method} {path} timed out") from exc
        except httpx.TransportError as exc:
            raise WriteError(f"jenkins transport error on {method} {path}: {exc}") from exc
        status = response.status_code
        if status in (401, 403):
            raise WriteAuthError(f"jenkins {method} {path}: HTTP {status}")
        if status == 404:
            raise WriteNotFoundError(f"jenkins {method} {path}: not found")
        if status == 429:
            raise WriteRateLimitError(f"jenkins {method} {path}: rate limited")
        if status >= 400:
            raise WriteError(f"jenkins {method} {path}: HTTP {status} - {response.text[:300]}")
        return response

    def _crumb(self) -> dict[str, str]:
        """CSRF crumb header (the capture-ci-fixture.sh pattern). A 404
        means the crumb issuer plugin is absent - no crumb needed."""
        try:
            data = self._request("GET", "/crumbIssuer/api/json").json()
        except WriteNotFoundError:
            return {}
        return {data["crumbRequestField"]: data["crumb"]}

    def trigger_build(self, job: str) -> dict[str, Any]:
        """POST /job/<job>/build with the CSRF crumb. Returns the queued
        item URL Jenkins reports via the Location header."""
        response = self._request("POST", f"/job/{job}/build", headers=self._crumb())
        return {
            "op": "trigger_build",
            "job": job,
            "status_code": response.status_code,
            "queue_url": response.headers.get("Location"),
            "origin": "live",
        }
