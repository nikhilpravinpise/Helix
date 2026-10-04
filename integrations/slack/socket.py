"""Slack Socket Mode: receive button clicks without a public URL.

Slack's interactive buttons normally POST to a public HTTPS endpoint
(`/slack/interactions`), which a laptop does not have. In Socket Mode Helix
opens the connection outward - `apps.connections.open` with an app-level
token (`xapp-`, scope `connections:write`) returns a WebSocket URL - and
Slack delivers each interaction over it as an envelope. The connection's
own authentication replaces the request signature, so the signing secret
is not involved.

Slack requires every envelope to be acknowledged within 3 seconds and
redelivers anything that is not, so `process_envelope` acks first and
handles second, and a handler that fails is logged without losing the ack.
Slack also recycles the connection every few hours and says so with a
`disconnect` message; the caller then opens a fresh one.
"""

from collections.abc import Callable
from typing import Any

import httpx

from helix.logging import get_logger
from integrations.slack.client import SlackError

CONNECTIONS_OPEN = "https://slack.com/api/apps.connections.open"


def open_connection_url(app_token: str, *, client: httpx.Client | None = None) -> str:
    """A fresh Socket Mode WebSocket URL for this app."""
    http = client or httpx.Client(timeout=10.0)
    response = http.post(CONNECTIONS_OPEN, headers={"Authorization": f"Bearer {app_token}"})
    body = response.json()
    if not body.get("ok"):
        raise SlackError(f"slack apps.connections.open failed: {body.get('error')}")
    return str(body["url"])


def process_envelope(
    message: dict[str, Any],
    *,
    send_ack: Callable[[dict[str, Any]], None],
    on_interactive: Callable[[dict[str, Any]], None],
) -> bool:
    """Handle one Socket Mode message. Returns False when Slack asks for a
    fresh connection, True to keep reading this one."""
    kind = message.get("type")
    if kind == "disconnect":
        return False
    envelope_id = message.get("envelope_id")
    if envelope_id is None:
        return True  # `hello` and other control messages carry nothing to ack
    send_ack({"envelope_id": envelope_id})
    if kind == "interactive":
        try:
            on_interactive(message.get("payload") or {})
        except Exception as exc:  # acked already; one bad click must not drop the socket
            get_logger().warning(
                "slack interaction failed", envelope_id=envelope_id, error=str(exc)
            )
    return True
