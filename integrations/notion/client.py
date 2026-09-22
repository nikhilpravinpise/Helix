"""The Notion transport contract (subphase 2.4).

CONTEXT.md section 10: Notion is durable human-readable incident
knowledge; section 13 is explicit that PostgreSQL is canonical and
Notion is a projection. A page is therefore rebuilt from provenance
(state/provenance.py) at sync time - never edited into truth.

Two implementations: `web.WebNotionClient` (the Notion REST API over
httpx) and `memory.MemoryNotionClient` (recording fake for fixtures and
tests). Blocks travel as Notion API block dicts; `pages.py` builds them,
so agents and the drain never assemble vendor payloads.
"""

from typing import Any, Protocol, runtime_checkable


class NotionError(RuntimeError):
    """A Notion call failed. One exception vocabulary for the outbox
    drain's error ledger, matching SlackError/WorkItemError."""


@runtime_checkable
class NotionClient(Protocol):
    """The three calls incident-page projection needs."""

    def create_page(
        self, parent_id: str, title: str, blocks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Create one page under `parent_id`. Returns the API payload
        (must include `id`; `url` when available)."""
        ...

    def append_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        """Append blocks to an existing page - how a page is "updated"
        as the incident lifecycle advances (the timeline grows; history
        is never rewritten)."""
        ...

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        """Fetch one page, or None if it does not exist - the drift
        check: a page deleted out from under Helix is recreated on the
        next sync rather than silently lost."""
        ...
