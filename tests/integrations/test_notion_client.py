"""Subphase 2.4 transport tests: `MemoryNotionClient` (the recording fake)
and `WebNotionClient` (the live REST client) over httpx.MockTransport."""

import json

import httpx
import pytest

from integrations.notion import MemoryNotionClient, NotionError, WebNotionClient
from integrations.notion.client import NotionClient


def _block(text: str = "hello") -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _web(handler) -> WebNotionClient:
    return WebNotionClient(
        "secret-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


class TestMemoryNotionClient:
    def test_satisfies_protocol(self):
        assert isinstance(MemoryNotionClient(), NotionClient)

    def test_create_page_stores_and_returns_ids(self):
        client = MemoryNotionClient()
        resp = client.create_page("parent-1", "INC-1: svc (received)", [_block()])

        assert resp == {"id": "mem-page-1", "url": "https://notion.so/mem-page-1"}
        assert client.pages["mem-page-1"] == {
            "title": "INC-1: svc (received)",
            "blocks": [_block()],
            "parent": "parent-1",
        }
        # A second page gets the next id - never a duplicate.
        resp2 = client.create_page("parent-1", "INC-2: svc (received)", [])
        assert resp2["id"] == "mem-page-2"

    def test_append_blocks_extends_in_order(self):
        client = MemoryNotionClient()
        page_id = client.create_page("p", "t", [_block("a")])["id"]
        client.append_blocks(page_id, [_block("b"), _block("c")])

        texts = [
            b["paragraph"]["rich_text"][0]["text"]["content"]
            for b in client.pages[page_id]["blocks"]
        ]
        assert texts == ["a", "b", "c"]

    def test_append_to_missing_page_raises(self):
        client = MemoryNotionClient()
        with pytest.raises(NotionError):
            client.append_blocks("mem-page-99", [_block()])

    def test_get_page_returns_stored_or_none(self):
        client = MemoryNotionClient()
        page_id = client.create_page("p", "t", [])["id"]

        assert client.get_page(page_id) is client.pages[page_id]
        assert client.get_page("mem-page-99") is None

    def test_calls_recorded_in_order(self):
        client = MemoryNotionClient()
        page_id = client.create_page("p", "t", [_block()])["id"]
        client.append_blocks(page_id, [_block()])
        client.get_page(page_id)

        assert [c["op"] for c in client.calls] == ["create_page", "append_blocks", "get_page"]

    def test_fail_with_raises_on_every_call(self):
        client = MemoryNotionClient()
        client.fail_with = NotionError("notion down")

        with pytest.raises(NotionError, match="notion down"):
            client.create_page("p", "t", [])
        with pytest.raises(NotionError, match="notion down"):
            client.append_blocks("x", [])
        with pytest.raises(NotionError, match="notion down"):
            client.get_page("x")
        # An outage records nothing - the send never happened.
        assert client.calls == []


class TestWebNotionClient:
    def test_satisfies_protocol(self):
        assert isinstance(_web(lambda r: httpx.Response(200, json={})), NotionClient)

    def test_create_page_posts_notion_shape(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["version"] = request.headers.get("notion-version")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "page-abc", "url": "https://notion.so/page-abc"})

        client = _web(handler)
        resp = client.create_page("parent-9", "INC-1: svc (received)", [_block()])

        assert resp["id"] == "page-abc"
        assert seen["method"] == "POST"
        assert seen["url"] == "https://api.notion.com/v1/pages"
        assert seen["auth"] == "Bearer secret-key"
        assert seen["version"] == "2022-06-28"
        assert seen["body"] == {
            "parent": {"page_id": "parent-9"},
            "properties": {
                "title": [{"type": "text", "text": {"content": "INC-1: svc (received)"}}]
            },
            "children": [_block()],
        }

    def test_append_blocks_patches_children(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"results": []})

        client = _web(handler)
        client.append_blocks("page-abc", [_block("x"), _block("y")])

        assert seen["method"] == "PATCH"
        assert seen["url"] == "https://api.notion.com/v1/blocks/page-abc/children"
        assert seen["body"] == {"children": [_block("x"), _block("y")]}

    def test_get_page_returns_payload(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert str(request.url) == "https://api.notion.com/v1/pages/page-abc"
            return httpx.Response(200, json={"id": "page-abc", "object": "page"})

        assert _web(handler).get_page("page-abc") == {"id": "page-abc", "object": "page"}

    def test_get_page_404_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"object": "error", "code": "object_not_found"})

        assert _web(handler).get_page("gone") is None

    @pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
    def test_http_errors_raise_notion_error(self, status):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"object": "error"})

        with pytest.raises(NotionError):
            _web(handler).create_page("p", "t", [])
        with pytest.raises(NotionError):
            _web(handler).append_blocks("p", [_block()])

    def test_transport_error_raises_notion_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with pytest.raises(NotionError, match="transport error"):
            _web(handler).get_page("page-abc")

    def test_timeout_raises_notion_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(NotionError, match="timed out"):
            _web(handler).create_page("p", "t", [])

    def test_children_chunked_at_notion_cap(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, str(request.url), json.loads(request.content)))
            if request.method == "POST":
                return httpx.Response(200, json={"id": "page-big"})
            return httpx.Response(200, json={"results": []})

        blocks = [_block(str(i)) for i in range(250)]
        _web(handler).create_page("p", "t", blocks)

        # 100 children ride along on create; the remaining 150 append in
        # two capped calls (100 + 50) against the new page id.
        assert len(calls) == 3
        assert len(calls[0][2]["children"]) == 100
        assert calls[1][1].endswith("/blocks/page-big/children")
        assert len(calls[1][2]["children"]) == 100
        assert len(calls[2][2]["children"]) == 50
