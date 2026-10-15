"""The Connections page speaks to team members, not to whoever runs the
deployment: no setting names, no "server", no "Server default" in the visible
text. What an admin needs lives inside a "For admins" disclosure."""

import re

import pytest
from sqlalchemy.orm import Session

from helix import connections_ui
from state import connections


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setenv("HELIX_PUBLIC_URL", "https://helix.example")
    monkeypatch.setenv("HELIX_SESSION_SECRET", "test-session-secret")
    for name in (
        "HELIX_CONNECTIONS_KEY",
        "HELIX_SLACK_CLIENT_ID",
        "HELIX_SLACK_CLIENT_SECRET",
        "HELIX_JIRA_OAUTH_CLIENT_ID",
        "HELIX_JIRA_OAUTH_CLIENT_SECRET",
        "HELIX_GITHUB_APP_ID",
        "HELIX_GITHUB_APP_SLUG",
        "HELIX_GITHUB_APP_PRIVATE_KEY",
        "HELIX_GITHUB_OAUTH_CLIENT_ID",
        "HELIX_CONNECTIONS_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HELIX_SLACK_BOT_TOKEN", "xoxb-not-real")
    monkeypatch.setenv("HELIX_JIRA_API_TOKEN", "not-real")
    monkeypatch.setenv("HELIX_GITHUB_TOKEN", "not-real")
    monkeypatch.setenv("HELIX_GITHUB_WRITES", "recorded")
    monkeypatch.setenv("HELIX_GITHUB_REPO", "acme/some-repo")


def _split(page: str) -> tuple[str, str]:
    """(visible text outside the admin disclosures, text inside them)."""
    body = page.split('class="pane-head"', 1)[1]
    admin = " ".join(re.findall(r"<details.*?</details>", body, flags=re.S))
    outside = re.sub(r"<details.*?</details>", " ", body, flags=re.S)
    outside = re.sub(r"<(script|style).*?</\1>", " ", outside, flags=re.S)
    outside = re.sub(r"<[^>]+>", " ", outside)
    return outside, admin


def _assert_plain(text: str) -> None:
    assert "HELIX_" not in text
    assert "server" not in text.lower()
    assert "Server default" not in text


def test_unconfigured_page_is_plain_and_admin_detail_is_in_the_disclosure(client):
    outside, admin = _split(client.get("/ui/connections").text)
    _assert_plain(outside)
    assert "An admin needs to finish setup" in outside
    assert "For admins" in admin
    assert "HELIX_CONNECTIONS_KEY" in admin


def test_each_missing_setting_is_named_for_admins_only(client, monkeypatch):
    monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "a key")
    monkeypatch.setenv("HELIX_SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_SLACK_CLIENT_SECRET", "secret")
    outside, admin = _split(client.get("/ui/connections").text)
    _assert_plain(outside)
    assert "HELIX_JIRA_OAUTH_CLIENT_ID" in admin
    assert "HELIX_GITHUB_APP_ID" in admin
    assert "HELIX_SLACK_CLIENT_ID" not in admin


def test_pr_bot_card_and_fallbacks_are_plain(client, sqlite_engine, monkeypatch):
    monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "a key")
    with Session(sqlite_engine) as session:
        connections.save_connection(
            session,
            "slack",
            account="Acme",
            scopes=["chat:write"],
            access_token="xoxb-not-real",
            refresh_token=None,
            expires_at=None,
            settings={"team_id": "T1"},
        )
        session.commit()
    outside, admin = _split(client.get("/ui/connections").text)
    _assert_plain(outside)
    assert "acme/some-repo (default)" in outside
    assert "Off (default)" in outside or "On (default)" in outside
    assert "HELIX_GITHUB_WRITES=recorded" in admin


@pytest.mark.parametrize("code", sorted(connections_ui._NOTICES))
def test_every_notice_is_plain(client, code):
    page = client.get(f"/ui/connections?notice={code}&detail=http_500").text
    notice = re.search(r'role="status">(.*?)</p>', page, flags=re.S)
    assert notice, code
    _assert_plain(notice.group(1))
