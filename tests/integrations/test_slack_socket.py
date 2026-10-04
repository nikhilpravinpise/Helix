"""Slack Socket Mode (`helix slack-socket`).

Socket Mode lets Slack deliver button clicks over a connection Helix opens
outward, so the Approve/Reject buttons work without a public URL into the
machine. Slack requires each envelope to be acknowledged within 3 seconds
and redelivers anything unacknowledged, so the ack goes out before the
click is handled, and a handler failure must never swallow it.
"""

import httpx
import pytest

from integrations.slack.client import SlackError
from integrations.slack.socket import open_connection_url, process_envelope


def _interactive(envelope_id="env-1"):
    return {
        "type": "interactive",
        "envelope_id": envelope_id,
        "payload": {"type": "block_actions", "actions": [{"action_id": "helix_review_approve:37"}]},
    }


class TestProcessEnvelope:
    def test_a_click_is_acked_before_it_is_handled(self):
        order = []
        keep_going = process_envelope(
            _interactive(),
            send_ack=lambda ack: order.append(("ack", ack)),
            on_interactive=lambda payload: order.append(("handled", payload["type"])),
        )
        assert keep_going is True
        assert order == [("ack", {"envelope_id": "env-1"}), ("handled", "block_actions")]

    def test_a_failing_handler_still_acks_and_keeps_the_connection(self):
        acks = []

        def boom(payload):
            raise RuntimeError("database gone")

        assert process_envelope(_interactive(), send_ack=acks.append, on_interactive=boom) is True
        assert acks == [{"envelope_id": "env-1"}]

    def test_other_envelopes_are_acked_but_not_handled(self):
        acks, handled = [], []
        process_envelope(
            {"type": "events_api", "envelope_id": "env-2", "payload": {}},
            send_ack=acks.append,
            on_interactive=handled.append,
        )
        assert acks == [{"envelope_id": "env-2"}]
        assert handled == []

    def test_hello_needs_no_ack(self):
        acks = []
        assert process_envelope({"type": "hello"}, send_ack=acks.append, on_interactive=print)
        assert acks == []

    def test_disconnect_asks_for_a_fresh_connection(self):
        """Slack recycles socket connections every few hours and announces
        it; the caller reconnects."""
        assert (
            process_envelope(
                {"type": "disconnect", "reason": "refresh_requested"},
                send_ack=print,
                on_interactive=print,
            )
            is False
        )


class TestOpenConnectionUrl:
    def _client(self, body, seen):
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=body)

        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_returns_the_websocket_url(self):
        seen = []
        url = open_connection_url(
            "xapp-1", client=self._client({"ok": True, "url": "wss://wss.slack.test/x"}, seen)
        )
        assert url == "wss://wss.slack.test/x"
        assert seen[0].url.path == "/api/apps.connections.open"
        assert seen[0].headers["authorization"] == "Bearer xapp-1"

    def test_a_refusal_raises_with_slacks_reason(self):
        with pytest.raises(SlackError, match="invalid_auth"):
            open_connection_url(
                "xapp-bad", client=self._client({"ok": False, "error": "invalid_auth"}, [])
            )
