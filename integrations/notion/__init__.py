"""The Notion knowledge projection (subphase 2.4).

CONTEXT.md section 13: PostgreSQL is the canonical state store; Notion is
durable human-readable incident knowledge projected FROM it - a page is
rebuilt from `state.provenance.assemble_provenance` at sync time, never
edited into truth. Every page effect reaches the client through the
`integration_outbox` drain (op `sync_page`), so a Notion outage degrades
the send, never the committed state.

`get_notion_client` resolves the configured implementation - None when
`notion_api_key` is unset, which the drain records as `skipped`
(delivered-to-nowhere is explicit, never a fake `sent`).
"""

from helix.config import get_settings
from integrations.notion.client import NotionClient, NotionError
from integrations.notion.memory import MemoryNotionClient
from integrations.notion.pages import build_incident_page
from integrations.notion.web import WebNotionClient

__all__ = [
    "MemoryNotionClient",
    "NotionClient",
    "NotionError",
    "WebNotionClient",
    "build_incident_page",
    "get_notion_client",
]


def get_notion_client() -> NotionClient | None:
    """Resolve the configured transport: the recording fake in fixture
    mode (the same role Slack's memory client plays - lets E2E and
    benchmark runs exercise the page pipeline without a live workspace),
    WebNotionClient when an API key is configured, None otherwise - the
    drain records that case as `skipped`, never a crash."""
    settings = get_settings()
    if settings.evidence_source == "fixtures":
        return MemoryNotionClient()
    if not settings.notion_api_key:
        return None
    return WebNotionClient(settings.notion_api_key)
