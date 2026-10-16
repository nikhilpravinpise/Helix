"""integrations/email.py: Resend's HTTPS API, faked with a MockTransport."""

import json

import httpx
import pytest

from integrations import email
from integrations.email import EmailError


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("HELIX_EMAIL_API_KEY", "re_secret_key")
    monkeypatch.setenv("HELIX_EMAIL_FROM", "Helix <invites@example.com>")


def _fake(monkeypatch, handler):
    monkeypatch.setattr(
        email, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    )


def test_send_posts_the_message_with_the_key_and_returns_the_id(configured, monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "em_123"})

    _fake(monkeypatch, handler)
    assert email.send_email("a@b.com", "Hi", "<p>x</p>", "x") == "em_123"
    request = seen[0]
    assert request.method == "POST" and str(request.url) == "https://api.resend.com/emails"
    assert request.headers["authorization"] == "Bearer re_secret_key"
    assert json.loads(request.content) == {
        "from": "Helix <invites@example.com>",
        "to": ["a@b.com"],
        "subject": "Hi",
        "html": "<p>x</p>",
        "text": "x",
    }


def test_a_4xx_is_an_email_error_with_the_status_code_and_never_the_key(configured, monkeypatch):
    _fake(monkeypatch, lambda r: httpx.Response(403, json={"message": "domain not verified"}))
    with pytest.raises(EmailError) as caught:
        email.send_email("a@b.com", "Hi", "<p>x</p>", "x")
    assert str(caught.value) == "http_403" and "re_secret_key" not in repr(caught.value)


def test_a_5xx_with_a_non_json_body_is_still_an_http_code(configured, monkeypatch):
    _fake(monkeypatch, lambda r: httpx.Response(502, text="bad gateway"))
    with pytest.raises(EmailError, match="http_502"):
        email.send_email("a@b.com", "Hi", "<p>x</p>", "x")


def test_a_success_without_an_id_is_an_error(configured, monkeypatch):
    _fake(monkeypatch, lambda r: httpx.Response(200, json={}))
    with pytest.raises(EmailError, match="non_json_response"):
        email.send_email("a@b.com", "Hi", "<p>x</p>", "x")


def test_a_transport_failure_is_an_email_error(configured, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("down")

    _fake(monkeypatch, handler)
    with pytest.raises(EmailError, match="transport_error"):
        email.send_email("a@b.com", "Hi", "<p>x</p>", "x")


def test_it_needs_both_the_key_and_the_sender(monkeypatch):
    monkeypatch.setenv("HELIX_EMAIL_API_KEY", "re_x")
    monkeypatch.setenv("HELIX_EMAIL_FROM", "")
    assert not email.is_configured()
    with pytest.raises(EmailError, match="not_configured"):
        email.send_email("a@b.com", "Hi", "<p>x</p>", "x")
