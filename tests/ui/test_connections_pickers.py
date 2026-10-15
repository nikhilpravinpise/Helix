"""Channel and project pickers on the Connections page (future_plans.md
Stage 1, slice 9).

Slack and Atlassian are faked with httpx.MockTransport behind
integrations.slack.oauth._http and integrations.work.jira_oauth._http; no
request leaves the test.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from helix import auth
from integrations.outbox import drain_outbox
from integrations.slack import oauth
from integrations.work import connected_jira_provider, jira_oauth
from integrations.work.contract import WorkItemSpec
from state import connections, models
from state.machine import IncidentStatus

BOT_TOKEN = "xoxb-secret-bot-token"
ACCESS = "jira-secret-access-token"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setenv("HELIX_PUBLIC_URL", "https://helix.example")
    monkeypatch.setenv("HELIX_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "test connections key")
    monkeypatch.setenv("HELIX_SLACK_CLIENT_ID", "slack-cid")
    monkeypatch.setenv("HELIX_SLACK_CLIENT_SECRET", "slack-csecret")
    monkeypatch.setenv("HELIX_JIRA_OAUTH_CLIENT_ID", "jira-cid")
    monkeypatch.setenv("HELIX_JIRA_OAUTH_CLIENT_SECRET", "jira-csecret")
    monkeypatch.setenv("HELIX_SLACK_DEFAULT_CHANNEL", "#from-env")
    monkeypatch.setenv("HELIX_JIRA_PROJECT_KEY", "ENVP")
    for name in (
        "HELIX_GITHUB_OAUTH_CLIENT_ID",
        "HELIX_SLACK_BOT_TOKEN",
        "HELIX_JIRA_API_TOKEN",
        "HELIX_CONNECTIONS_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def https(client):
    client.base_url = "https://helix.example"


@pytest.fixture
def slack(monkeypatch):
    """Fake Slack Web API. `pages` is the list of conversations.list replies
    served in order by cursor; `calls` records (method, form)."""
    calls: list[tuple[str, dict]] = []
    state = {
        "pages": [
            {
                "ok": True,
                "channels": [
                    {"id": "C1", "name": "general"},
                    {"id": "C2", "name": "ops"},
                ],
                "response_metadata": {"next_cursor": "page2"},
            },
            {
                "ok": True,
                "channels": [{"id": "C3", "name": "helix-incidents"}],
                "response_metadata": {"next_cursor": ""},
            },
        ],
        "error": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        calls.append((method, form))
        if state["error"] == "transport":
            raise httpx.ConnectError("down")
        if state["error"]:
            return httpx.Response(200, json={"ok": False, "error": state["error"]})
        index = 1 if form.get("cursor") == "page2" else 0
        return httpx.Response(200, json=state["pages"][index])

    monkeypatch.setattr(
        oauth, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    )
    return type("Slack", (), {"calls": calls, "state": state})


@pytest.fixture
def atlassian(monkeypatch):
    """Fake Atlassian. Project search is served 2 per page by startAt."""
    calls: list[dict] = []
    projects = [
        {"key": "OPS", "name": "Operations"},
        {"key": "WEB", "name": "Website"},
        {"key": "PAY", "name": "Payments"},
    ]
    state = {"status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        path = "/" + request.url.path.split("/", 4)[4] if "/ex/jira/" in request.url.path else ""
        calls.append(
            {
                "method": request.method,
                "path": path,
                "query": dict(request.url.params),
                "auth": request.headers.get("authorization", ""),
                "body": json.loads(request.content) if request.content else None,
            }
        )
        if path == "/rest/api/3/project/search":
            if state["status"] != 200:
                return httpx.Response(state["status"], json={})
            start = int(request.url.params.get("startAt", 0))
            page = projects[start : start + 2]
            return httpx.Response(
                200, json={"values": page, "isLast": start + 2 >= len(projects), "total": 3}
            )
        if path == "/rest/api/3/issue":
            return httpx.Response(201, json={"id": "10001", "key": "PAY-1"})
        return httpx.Response(404, json={})

    monkeypatch.setattr(
        jira_oauth,
        "_http",
        lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=5),
    )
    return type("Atlassian", (), {"calls": calls, "state": state})


def _save_slack(sqlite_engine, scopes=None, **settings):
    with Session(sqlite_engine) as session:
        connections.save_connection(
            session,
            "slack",
            account="Acme",
            scopes=scopes or ["chat:write", "channels:read"],
            access_token=BOT_TOKEN,
            refresh_token=None,
            expires_at=None,
            settings={"team_id": "T1", **settings},
        )
        session.commit()


def _save_jira(sqlite_engine, **settings):
    with Session(sqlite_engine) as session:
        connections.save_connection(
            session,
            "jira",
            account="Vraj Ved",
            scopes=["read:jira-work", "write:jira-work"],
            access_token=ACCESS,
            refresh_token="r",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            settings={
                "cloud_id": "cloud-1",
                "site_url": "https://acme.atlassian.net",
                "site_name": "acme",
                **settings,
            },
        )
        session.commit()


def _stored(sqlite_engine, provider):
    with Session(sqlite_engine) as session:
        return connections.get_connection(session, provider)


def _card(page: str, provider: str) -> str:
    return page.split(f'id="{provider}"')[1].split("</section>")[0]


def _select(card: str) -> str:
    return card.split("<select")[1].split("</select>")[0]


# -- Slack ------------------------------------------------------------


def test_slack_card_lists_channels_following_pagination(client, slack, sqlite_engine):
    _save_slack(sqlite_engine, channel="C2")
    card = _card(client.get("/ui/connections").text, "slack")
    options = _select(card)
    assert "#general" in options and "#ops" in options and "#helix-incidents" in options
    assert [c[0] for c in slack.calls] == ["conversations.list"] * 2
    first = slack.calls[0][1]
    assert first["types"] == "public_channel" and first["exclude_archived"] == "true"
    assert slack.calls[1][1]["cursor"] == "page2"
    assert BOT_TOKEN not in card


def test_slack_current_channel_is_selected(client, slack, sqlite_engine):
    _save_slack(sqlite_engine, channel="C2")
    options = _select(_card(client.get("/ui/connections").text, "slack"))
    assert 'value="C2" selected' in options
    assert options.count("selected") == 1


def test_slack_without_a_choice_shows_what_will_be_used(client, slack, sqlite_engine):
    _save_slack(sqlite_engine)
    card = _card(client.get("/ui/connections").text, "slack")
    assert "#from-env" in card


def test_slack_save_stores_the_id_and_the_outbox_posts_to_it(client, slack, sqlite_engine):
    _save_slack(sqlite_engine)
    response = client.post(
        "/ui/connections/slack/settings", data={"channel": "C3"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/connections?notice=")
    stored = _stored(sqlite_engine, "slack")
    assert stored.settings["channel"] == "C3" and stored.settings["team_id"] == "T1"
    assert stored.access_token == BOT_TOKEN

    posted: list[str] = []

    class _Slack:
        def post_message(self, channel, text, *, thread_ts=None, blocks=None):
            posted.append(channel)
            return {"ok": True, "ts": "1.1"}

        def update_message(self, channel, ts, text, *, blocks=None):
            return {"ok": True, "ts": ts}

    with Session(sqlite_engine) as session:
        incident = models.Incident(
            incident_id=f"INC-{uuid.uuid4().hex[:8]}",
            kind=models.IncidentKind("change"),
            service="payment-api",
            environment="sandbox",
            severity="high",
            status=IncidentStatus.RECEIVED,
            fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
        )
        session.add(incident)
        session.flush()
        session.add(
            models.IntegrationOutbox(
                incident_id=incident.id, integration="slack", op="notify", payload={}
            )
        )
        session.flush()
        drain_outbox(session, slack=_Slack())
        assert session.scalars(select(models.IntegrationOutbox)).one().status == "sent"
    assert posted == ["C3"]


@pytest.mark.parametrize("value", ["C999", "#general", "", "C1; drop"])
def test_slack_unlisted_value_is_refused(client, slack, sqlite_engine, value):
    _save_slack(sqlite_engine, channel="C2")
    response = client.post(
        "/ui/connections/slack/settings", data={"channel": value}, follow_redirects=False
    )
    assert response.status_code == 303
    assert _stored(sqlite_engine, "slack").settings["channel"] == "C2"


def test_slack_save_when_the_list_cannot_load_changes_nothing(client, slack, sqlite_engine):
    _save_slack(sqlite_engine, channel="C2")
    slack.state["error"] = "transport"
    client.post("/ui/connections/slack/settings", data={"channel": "C1"})
    assert _stored(sqlite_engine, "slack").settings["channel"] == "C2"


def test_slack_save_when_not_connected_does_nothing(client, slack, sqlite_engine):
    response = client.post(
        "/ui/connections/slack/settings", data={"channel": "C1"}, follow_redirects=False
    )
    assert response.status_code == 303 and slack.calls == []
    assert _stored(sqlite_engine, "slack") is None


def test_slack_missing_scope_suggests_reconnect(client, slack, sqlite_engine):
    _save_slack(sqlite_engine)
    slack.state["error"] = "missing_scope"
    page = client.get("/ui/connections")
    assert page.status_code == 200
    card = _card(page.text, "slack")
    assert "Reconnect" in card and "<select" not in card
    assert "channels:read" in card


def test_slack_list_failure_still_renders_with_a_note(client, slack, sqlite_engine):
    _save_slack(sqlite_engine, channel="C2")
    slack.state["error"] = "transport"
    page = client.get("/ui/connections")
    assert page.status_code == 200
    card = _card(page.text, "slack")
    assert "could not load" in card.lower()
    assert "C2" in card and "/ui/connections/slack/disconnect" in card


def test_slack_not_connected_has_no_picker_and_no_fetch(client, slack):
    card = _card(client.get("/ui/connections").text, "slack")
    assert "<select" not in card and slack.calls == []


# -- Jira -------------------------------------------------------------


def test_jira_card_lists_projects_following_pagination(client, atlassian, sqlite_engine):
    _save_jira(sqlite_engine, project_key="WEB")
    card = _card(client.get("/ui/connections").text, "jira")
    options = _select(card)
    assert "OPS - Operations" in options and "WEB - Website" in options
    assert "PAY - Payments" in options
    searches = [c for c in atlassian.calls if c["path"] == "/rest/api/3/project/search"]
    assert [c["query"]["startAt"] for c in searches] == ["0", "2"]
    assert searches[0]["auth"] == f"Bearer {ACCESS}"
    assert 'value="WEB" selected' in options
    assert ACCESS not in card


def test_jira_without_a_choice_shows_what_will_be_used(client, atlassian, sqlite_engine):
    _save_jira(sqlite_engine)
    assert "ENVP" in _card(client.get("/ui/connections").text, "jira")


def test_jira_save_stores_project_key_and_issues_are_created_there(
    client, atlassian, sqlite_engine
):
    _save_jira(sqlite_engine)
    response = client.post(
        "/ui/connections/jira/settings", data={"project_key": "PAY"}, follow_redirects=False
    )
    assert response.status_code == 303
    stored = _stored(sqlite_engine, "jira")
    assert stored.settings["project_key"] == "PAY"
    assert stored.settings["cloud_id"] == "cloud-1"

    with Session(sqlite_engine) as session:
        provider = connected_jira_provider(session, client=jira_oauth._http())
        provider.create_work_item(
            WorkItemSpec(incident_id="INC-1", title="t", description="d", priority=None)
        )
    issue = [c for c in atlassian.calls if c["path"] == "/rest/api/3/issue"][0]
    assert issue["body"]["fields"]["project"] == {"key": "PAY"}


@pytest.mark.parametrize("value", ["NOPE", "ops", "", "OPS\nX"])
def test_jira_unlisted_key_is_refused(client, atlassian, sqlite_engine, value):
    _save_jira(sqlite_engine, project_key="OPS")
    response = client.post(
        "/ui/connections/jira/settings", data={"project_key": value}, follow_redirects=False
    )
    assert response.status_code == 303
    assert _stored(sqlite_engine, "jira").settings["project_key"] == "OPS"


def test_jira_list_failure_still_renders_with_a_note(client, atlassian, sqlite_engine):
    _save_jira(sqlite_engine, project_key="OPS")
    atlassian.state["status"] = 500
    page = client.get("/ui/connections")
    assert page.status_code == 200
    card = _card(page.text, "jira")
    assert "could not load" in card.lower() and "OPS" in card
    assert "/ui/connections/jira/disconnect" in card


def test_jira_not_connected_has_no_picker_and_no_fetch(client, atlassian):
    card = _card(client.get("/ui/connections").text, "jira")
    assert "<select" not in card and atlassian.calls == []
    response = client.post(
        "/ui/connections/jira/settings", data={"project_key": "OPS"}, follow_redirects=False
    )
    assert response.status_code == 303 and atlassian.calls == []


# -- sign-in ----------------------------------------------------------


def test_settings_routes_require_sign_in(client, monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "VrajVed")
    for path, data in [
        ("/ui/connections/slack/settings", {"channel": "C1"}),
        ("/ui/connections/jira/settings", {"project_key": "OPS"}),
    ]:
        response = client.post(path, data=data, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].endswith("/login"), path


def test_signed_in_user_can_save(client, slack, sqlite_engine, monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "VrajVed")
    monkeypatch.setattr(auth, "github_login", lambda code, redirect_uri: "VrajVed")
    start = client.get("/auth/github", follow_redirects=False)
    gh_state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    client.get(f"/auth/github/callback?code=c&state={gh_state}", follow_redirects=False)
    _save_slack(sqlite_engine)
    client.post("/ui/connections/slack/settings", data={"channel": "C1"}, follow_redirects=False)
    assert _stored(sqlite_engine, "slack").settings["channel"] == "C1"
