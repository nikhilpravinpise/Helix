"""Slack calls that find a person by email and message them (team Slack
matching). Slack is faked behind httpx.MockTransport on the oauth `_http`
seam; nothing leaves the test."""

import httpx
import pytest

from integrations.slack import SlackError, oauth


@pytest.fixture
def slack(monkeypatch):
    calls: list[tuple[str, dict, str]] = []
    reply: dict = {"ok": True}

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(pair.split("=", 1) for pair in request.content.decode().split("&") if pair)
        calls.append((request.url.path.rsplit("/", 1)[-1], form, request.headers["authorization"]))
        return httpx.Response(200, json=reply)

    monkeypatch.setattr(
        oauth, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    )
    return type("Slack", (), {"calls": calls, "reply": reply})


def test_scopes_include_the_email_lookup_scopes():
    scopes = oauth.SCOPES.split(",")
    assert "users:read" in scopes and "users:read.email" in scopes
    assert {"chat:write", "chat:write.public", "channels:read"} <= set(scopes)


def test_lookup_by_email_returns_the_member_id(slack):
    slack.reply.update(user={"id": "U42"})
    assert oauth.lookup_user_by_email("xoxb-t", "a%40x.com".replace("%40", "@")) == "U42"
    method, form, auth = slack.calls[0]
    assert method == "users.lookupByEmail" and form["email"] == "a%40x.com"
    assert auth == "Bearer xoxb-t"


def test_lookup_by_email_not_found_is_none(slack):
    slack.reply.update(ok=False, error="users_not_found")
    assert oauth.lookup_user_by_email("xoxb-t", "who@x.com") is None


def test_lookup_by_email_missing_scope_raises(slack):
    slack.reply.update(ok=False, error="missing_scope")
    with pytest.raises(SlackError, match="missing_scope"):
        oauth.lookup_user_by_email("xoxb-t", "a@x.com")


def test_lookup_by_email_other_errors_raise(slack):
    slack.reply.update(ok=False, error="ratelimited")
    with pytest.raises(SlackError, match="ratelimited"):
        oauth.lookup_user_by_email("xoxb-t", "a@x.com")


def test_send_dm_posts_to_the_user_id(slack):
    oauth.send_dm("xoxb-t", "U42", "hello there")
    method, form, _ = slack.calls[0]
    assert method == "chat.postMessage"
    assert form["channel"] == "U42" and form["text"] == "hello+there"


def test_send_dm_failure_raises(slack):
    slack.reply.update(ok=False, error="channel_not_found")
    with pytest.raises(SlackError, match="channel_not_found"):
        oauth.send_dm("xoxb-t", "U42", "hi")
