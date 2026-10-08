"""SlackClient implementation tests (subphase 2.2).

The memory fake's recording contract (what the drain tests and fixtures
inspect), and the web client's request shape plus its single SlackError
vocabulary - checked over httpx.MockTransport, no network.
"""

import json

import httpx
import pytest

from integrations.slack import (
    MemorySlackClient,
    SlackClient,
    SlackError,
    WebSlackClient,
    get_slack_client,
)


class TestMemorySlackClient:
    def test_conforms_to_protocol(self):
        assert isinstance(MemorySlackClient(), SlackClient)

    def test_post_message_records_call_and_returns_ts(self):
        client = MemorySlackClient()
        resp = client.post_message(
            "#incidents", "INC-1 update", thread_ts="111.222", blocks=[{"type": "section"}]
        )
        assert resp["ok"] is True and resp["ts"]
        assert client.calls == [
            {
                "op": "post_message",
                "channel": "#incidents",
                "text": "INC-1 update",
                "thread_ts": "111.222",
                "blocks": [{"type": "section"}],
                "ts": resp["ts"],
            }
        ]

    def test_ts_increments_per_call(self):
        client = MemorySlackClient()
        first = client.post_message("#c", "one")["ts"]
        second = client.post_message("#c", "two")["ts"]
        assert first != second

    def test_update_message_records_call(self):
        client = MemorySlackClient()
        resp = client.update_message("#c", "111.222", "updated")
        assert resp["ok"] is True
        assert client.calls[0]["op"] == "update_message"
        assert client.calls[0]["channel"] == "#c"
        assert client.calls[0]["ts"] == "111.222"
        assert client.calls[0]["text"] == "updated"

    def test_fail_with_raises_on_every_method(self):
        client = MemorySlackClient()
        client.fail_with = SlackError("slack is down")
        with pytest.raises(SlackError, match="slack is down"):
            client.post_message("#c", "x")
        with pytest.raises(SlackError, match="slack is down"):
            client.update_message("#c", "1.2", "x")
        assert client.calls == []


def _web_client(handler) -> WebSlackClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://slack.com/api")
    return WebSlackClient("xoxb-test-token", client=http)


class TestWebSlackClient:
    def test_conforms_to_protocol(self):
        assert isinstance(
            _web_client(lambda r: httpx.Response(200, json={"ok": True})), SlackClient
        )

    def test_post_message_request_shape(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["json"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "ts": "1700.1"})

        client = _web_client(handler)
        resp = client.post_message(
            "#incidents", "hi", thread_ts="111.222", blocks=[{"type": "actions"}]
        )
        assert resp == {"ok": True, "ts": "1700.1"}
        assert seen["url"] == "https://slack.com/api/chat.postMessage"
        assert seen["auth"] == "Bearer xoxb-test-token"
        assert seen["json"] == {
            "channel": "#incidents",
            "text": "hi",
            "thread_ts": "111.222",
            "blocks": [{"type": "actions"}],
        }

    def test_post_message_omits_optional_fields(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["json"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "ts": "1.2"})

        _web_client(handler).post_message("#c", "hi")
        assert seen["json"] == {"channel": "#c", "text": "hi"}

    def test_update_message_posts_chat_update(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["json"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "ts": "9.9"})

        _web_client(handler).update_message("#c", "111.222", "done")
        assert seen["url"] == "https://slack.com/api/chat.update"
        assert seen["json"] == {"channel": "#c", "ts": "111.222", "text": "done"}

    def test_ok_false_raises_slack_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

        with pytest.raises(SlackError, match="channel_not_found"):
            _web_client(handler).post_message("#nope", "hi")

    def test_http_error_raises_slack_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        with pytest.raises(SlackError, match="HTTP 500"):
            _web_client(handler).post_message("#c", "hi")

    def test_transport_error_raises_slack_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(SlackError, match="transport error"):
            _web_client(handler).post_message("#c", "hi")


class TestGetSlackClient:
    def test_fixture_evidence_does_not_fake_slack(self, monkeypatch):
        """Decided 2026-10-06 (Vraj): where evidence comes from does not
        decide where messages go. A live Slack test on recorded cluster
        evidence posted to a fake and nothing reached the channel. Tests
        and the benchmark stay offline by blanking the token instead."""
        monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
        monkeypatch.setenv("HELIX_SLACK_BOT_TOKEN", "xoxb-live")
        assert isinstance(get_slack_client(), WebSlackClient)

    def test_fixture_mode_without_token_returns_none(self, monkeypatch):
        monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
        monkeypatch.setenv("HELIX_SLACK_BOT_TOKEN", "")
        assert get_slack_client() is None

    def test_live_mode_with_token_returns_web_client(self, monkeypatch):
        monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "live")
        monkeypatch.setenv("HELIX_SLACK_BOT_TOKEN", "xoxb-live")
        assert isinstance(get_slack_client(), WebSlackClient)

    def test_live_mode_without_token_returns_none(self, monkeypatch):
        monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "live")
        monkeypatch.setenv("HELIX_SLACK_BOT_TOKEN", "")
        assert get_slack_client() is None
