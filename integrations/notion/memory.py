"""The recording fake Notion client (subphase 2.4).

Same role `integrations/fakes.py` plays for the Phase 1 write transports:
unit tests and fixture-mode runs need a `NotionClient` that behaves like
the real API - pages exist once created, appends accumulate in order, an
append to a missing page is an error, a deleted page reads None - without
touching the network.

`.pages` is the inspectable store, `.calls` the ordered audit the drain
tests assert against, and `fail_with` simulates the Notion outage the
outbox must degrade gracefully through (an exception set there is raised
by every call, before anything is recorded).
"""

import uuid
from typing import Any

from integrations.notion.client import NotionError


class MemoryNotionClient:
    """In-memory `NotionClient`: a dict of pages keyed by page id."""

    def __init__(self) -> None:
        #: {page_id: {"title": ..., "blocks": [...], "parent": ...}} - the
        #: whole fake store, inspectable in tests.
        self.pages: dict[str, dict[str, Any]] = {}
        #: Every call, in order: {"op": ..., call args...}.
        self.calls: list[dict[str, Any]] = []
        #: Set to an exception and every call raises it (outage sim).
        self.fail_with: Exception | None = None

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def create_page(
        self, parent_id: str, title: str, blocks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self._maybe_fail()
        # Unique across processes, not just this instance: every `helix`
        # command builds its own client, and a per-instance counter gave
        # every incident's page "mem-page-1" (MemoryWorkProvider had the same
        # defect). Random so a recreated page gets a new id, as Notion's would.
        page_id = f"mem-page-{uuid.uuid4().hex[:12]}"
        self.pages[page_id] = {
            "title": title,
            "blocks": list(blocks),
            "parent": parent_id,
        }
        self.calls.append(
            {
                "op": "create_page",
                "parent_id": parent_id,
                "title": title,
                "blocks": list(blocks),
            }
        )
        return {"id": page_id, "url": f"https://notion.so/{page_id}"}

    def append_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        self._maybe_fail()
        page = self.pages.get(page_id)
        if page is None:
            # The real API 404s on an append to a page that does not
            # exist; the fake stays honest about the same bound.
            raise NotionError(f"no such page {page_id}")
        page["blocks"].extend(blocks)
        self.calls.append({"op": "append_blocks", "page_id": page_id, "blocks": list(blocks)})
        return {"id": page_id, "appended": len(blocks)}

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        self._maybe_fail()
        self.calls.append({"op": "get_page", "page_id": page_id})
        return self.pages.get(page_id)
