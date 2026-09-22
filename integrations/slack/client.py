"""The Slack transport contract (subphase 2.2).

CONTEXT.md section 10: Slack is human communication, approval,
escalation and status - and is NOT the canonical database. Every Slack
effect therefore reaches this client only through the
`integration_outbox` drain (integrations/outbox.py): the durable row is
committed with the state change, the send is a projection of it.

Two implementations: `web.WebSlackClient` (the real Slack Web API over
httpx) and `memory.MemorySlackClient` (recording fake for fixtures and
tests, the same role `integrations/fakes.py` plays for the Phase 1 write
transports).
"""

from typing import Any, Protocol, runtime_checkable


class SlackError(RuntimeError):
    """A Slack call failed (auth, transport, API-level `ok: false`).
    One exception vocabulary for the outbox drain's error ledger, the
    same role `WorkItemError` plays for work providers."""


@runtime_checkable
class SlackClient(Protocol):
    """The two calls Helix needs. Returned dicts carry at least `ts`
    (Slack's message id) - the drain stores it so later updates land in
    the incident's thread instead of spamming the channel."""

    def post_message(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Post one message; `thread_ts` replies inside an existing
        incident thread. Returns the API payload (must include `ts`)."""
        ...

    def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Edit a previously posted message (e.g. an approval request
        once it has been decided)."""
        ...
