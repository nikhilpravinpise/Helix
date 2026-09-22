"""The live Notion REST client (subphase 2.4).

Thin httpx wrapper over the three endpoints the incident-page projection
needs: `POST /pages` (create), `PATCH /blocks/{id}/children` (append - how
a page "updates" as the lifecycle advances; history is never rewritten),
and `GET /pages/{id}` (the drift check - a page deleted in Notion reads
as 404 so the drain recreates it rather than appending blind).

Notion caps a children array at 100 blocks per call; pages.py clips
rich_text to ~1900 chars, this client chunks appends - both vendor
bounds are enforced at the boundary so the drain never sees a validation
error. Failures land in the NotionError vocabulary the outbox ledger
records, the same role SlackError/WorkItemError play.
"""

from typing import Any

import httpx

from integrations.evidence import EvidenceError
from integrations.http import raise_for_status
from integrations.notion.client import NotionError

#: Notion rejects children arrays longer than this on create and append.
_MAX_CHILDREN = 100


class WebNotionClient:
    """`NotionClient` over the real REST API.

    `client` is the injection seam for tests (an httpx.Client over a
    MockTransport); a real one is created lazily on first call so
    importing/constructing never touches the network or needs a token.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.notion.com/v1",
        notion_version: str = "2022-06-28",
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._version = notion_version
        self._client = client
        self._timeout = timeout

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _send(self, method: str, path: str, *, json: dict | None = None) -> httpx.Response:
        """One raw call. Transport failures -> NotionError here; status
        mapping stays with the caller because get_page needs 404 as a
        value (page gone), not an error."""
        try:
            return self._http().request(
                method,
                f"{self._base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Notion-Version": self._version,
                },
                json=json,
            )
        except httpx.TimeoutException as exc:
            raise NotionError(f"notion {method} {path} timed out") from exc
        except httpx.HTTPError as exc:
            raise NotionError(f"notion transport error on {method} {path}: {exc}") from exc

    def _request(self, method: str, path: str, *, json: dict | None = None) -> dict[str, Any]:
        """A call that must succeed: HTTP errors -> NotionError via the
        shared raise_for_status vocabulary (integrations/http.py)."""
        response = self._send(method, path, json=json)
        try:
            raise_for_status(response)
        except EvidenceError as exc:
            raise NotionError(str(exc)) from exc
        return response.json()

    def create_page(
        self, parent_id: str, title: str, blocks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """POST /pages under `parent_id`. Children past the first 100 are
        appended right after creation - same page, extra call, no cap hit."""
        page = self._request(
            "POST",
            "/pages",
            json={
                "parent": {"page_id": parent_id},
                "properties": {"title": [{"type": "text", "text": {"content": title}}]},
                "children": blocks[:_MAX_CHILDREN],
            },
        )
        rest = blocks[_MAX_CHILDREN:]
        for i in range(0, len(rest), _MAX_CHILDREN):
            self.append_blocks(str(page["id"]), rest[i : i + _MAX_CHILDREN])
        return page

    def append_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        """PATCH /blocks/{page_id}/children, chunked at the 100-block cap."""
        result: dict[str, Any] = {}
        for i in range(0, len(blocks), _MAX_CHILDREN):
            result = self._request(
                "PATCH",
                f"/blocks/{page_id}/children",
                json={"children": blocks[i : i + _MAX_CHILDREN]},
            )
        return result

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        """GET /pages/{page_id}; 404 -> None, the drain's recreate signal."""
        response = self._send("GET", f"/pages/{page_id}")
        if response.status_code == 404:
            return None
        try:
            raise_for_status(response)
        except EvidenceError as exc:
            raise NotionError(str(exc)) from exc
        return response.json()
