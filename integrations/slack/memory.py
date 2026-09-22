"""The recording Slack transport (subphase 2.2).

The fixture-mode and test double for SlackClient - the same role
integrations/fakes.py plays for the Phase 1 write transports: every
intended send lands in `.calls` so tests and the benchmark can inspect
"the message Helix would have sent" without a workspace.

`fail_with` simulates the outage the outbox drain's retry ledger exists
for: set it and every call raises, leaving the row pending rather than
lost - Slack is never the database.
"""

from typing import Any


class MemorySlackClient:
    """Records every call; returns canned Slack-shaped successes."""

    def __init__(self) -> None:
        #: Every intended message, in order: {"op", "channel", "text",
        #: "thread_ts", "blocks", "ts"}.
        self.calls: list[dict[str, Any]] = []
        #: When set, every method raises it - the outage simulation the
        #: drain tests rely on.
        self.fail_with: Exception | None = None
        self._next = 0

    def _record(self, op: str, **fields: Any) -> dict[str, Any]:
        if self.fail_with is not None:
            raise self.fail_with
        self._next += 1
        ts = f"1700000000.{self._next:06d}"  # Slack ts shape: <epoch>.<seq>
        call = {"op": op, "ts": ts, **fields}
        self.calls.append(call)
        return {"ok": True, "ts": ts}

    def post_message(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self._record(
            "post_message",
            channel=channel,
            text=text,
            thread_ts=thread_ts,
            blocks=blocks,
        )

    def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self._record(
            "update_message",
            channel=channel,
            ts=ts,
            text=text,
            thread_ts=None,
            blocks=blocks,
        )
