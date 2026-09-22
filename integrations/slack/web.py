"""The live Slack Web API transport (subphase 2.2).

Implements the SlackClient protocol against chat.postMessage /
chat.update over httpx. The error vocabulary is SlackError alone -
transport failures, HTTP errors and Slack's own `ok: false` envelope all
collapse into the one type the outbox drain's retry ledger records, the
same role integrations/http.py's EvidenceError mapping plays for the
evidence capabilities.
"""

from typing import Any

import httpx

from integrations.slack.client import SlackError

API_ROOT = "https://slack.com/api"


class WebSlackClient:
    """Slack Web API client. `client` is the injection seam for tests -
    pass an httpx.Client on a MockTransport and no network is touched."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_ROOT,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST one Web API method; every failure is a SlackError so the
        drain's ledger has one vocabulary to record."""
        try:
            response = self._client.post(
                f"{self._base_url}/{method}",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            raise SlackError(f"slack {method} transport error: {exc}") from exc
        if response.status_code >= 400:
            raise SlackError(f"slack {method} HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise SlackError(f"slack {method} returned non-JSON") from exc
        # Slack answers HTTP 200 with ok:false for API-level failures
        # (bad auth, unknown channel) - an error is an error either way.
        if not data.get("ok"):
            raise SlackError(f"slack {method} failed: {data.get('error', 'unknown_error')}")
        return data

    def post_message(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts is not None:
            payload["thread_ts"] = thread_ts
        if blocks is not None:
            payload["blocks"] = blocks
        return self._post("chat.postMessage", payload)

    def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
        if blocks is not None:
            payload["blocks"] = blocks
        return self._post("chat.update", payload)
