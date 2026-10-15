"""Connect Jira on the Connections page (future_plans.md Stage 1, slice 7).

Atlassian is faked with httpx.MockTransport behind
integrations.work.jira_oauth._http; no request leaves the test.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy.orm import Session

from integrations.lifecycle_sync import enqueue_for_transition
from integrations.work import effective_work_provider, jira_oauth
from state import connections, models
from state.machine import IncidentStatus

CALLBACK = "https://helix.example/ui/connections/jira/callback"
ACCESS = "jira-secret-access-token"
REFRESH = "jira-secret-refresh-token"
SITE = {
    "id": "cloud-1",
    "url": "https://acme.atlassian.net",
    "name": "acme",
    "scopes": ["read:jira-work", "write:jira-work"],
}
TOKEN_OK = {
    "access_token": ACCESS,
    "refresh_token": REFRESH,
    "expires_in": 3600,
    "scope": "read:jira-work write:jira-work read:jira-user offline_access read:me",
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setenv("HELIX_PUBLIC_URL", "https://helix.example")
    monkeypatch.setenv("HELIX_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "test connections key")
    monkeypatch.setenv("HELIX_JIRA_OAUTH_CLIENT_ID", "jira-cid")
    monkeypatch.setenv("HELIX_JIRA_OAUTH_CLIENT_SECRET", "jira-csecret")
    for name in (
        "HELIX_GITHUB_OAUTH_CLIENT_ID",
        "HELIX_JIRA_API_TOKEN",
        "HELIX_CONNECTIONS_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def https(client):
    client.base_url = "https://helix.example"


@pytest.fixture
def atlassian(monkeypatch):
    """Fake Atlassian: `replies` maps a URL path to (status, JSON)."""
    calls: list[dict] = []
    replies: dict[str, tuple[int, object]] = {
        "/oauth/token": (200, TOKEN_OK),
        "/oauth/token/accessible-resources": (200, [SITE]),
        "/me": (200, {"name": "Vraj Ved", "email": "vraj@acme.test"}),
        "/rest/api/3/myself": (200, {"displayName": "Vraj Ved"}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/ex/jira/"):
            path = path.split("/", 4)[4]
            path = "/" + path
        calls.append(
            {
                "method": request.method,
                "path": path,
                "auth": request.headers.get("authorization", ""),
                "body": json.loads(request.content) if request.content else None,
            }
        )
        status, body = replies.get(path, (404, {}))
        return httpx.Response(status, json=body)

    monkeypatch.setattr(
        jira_oauth,
        "_http",
        lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=5),
    )
    return type("Atlassian", (), {"calls": calls, "replies": replies})


def _start(client) -> str:
    response = client.post("/ui/connections/jira/connect", follow_redirects=False)
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


def _callback(client, state=None, query="code=abc"):
    state = state or _start(client)
    return client.get(
        f"/ui/connections/jira/callback?{query}&state={state}", follow_redirects=False
    )


def _stored(sqlite_engine):
    with Session(sqlite_engine) as session:
        return connections.get_connection(session, "jira")


def _save(sqlite_engine, *, expires_at=None, **settings):
    with Session(sqlite_engine) as session:
        connections.save_connection(
            session,
            "jira",
            account="Vraj Ved",
            scopes=["read:jira-work", "write:jira-work"],
            access_token=ACCESS,
            refresh_token=REFRESH,
            expires_at=expires_at or datetime.now(UTC) + timedelta(hours=1),
            settings={
                "cloud_id": "cloud-1",
                "site_url": "https://acme.atlassian.net",
                "site_name": "acme",
                **settings,
            },
        )
        session.commit()


def _jira_card(page: str) -> str:
    return page.split('id="jira"')[1].split("</section>")[0]


def test_card_not_connected_offers_connect(client):
    card = _jira_card(client.get("/ui/connections").text)
    assert "Jira" in card and "Not connected" in card
    assert 'action="/ui/connections/jira/connect"' in card
    assert "disabled" not in card.split("jira/connect")[1].split("</form>")[0]


def test_card_not_connected_with_server_token_says_so(client, monkeypatch):
    monkeypatch.setenv("HELIX_JIRA_API_TOKEN", "server-api-token")
    card = _jira_card(client.get("/ui/connections").text)
    assert "Not connected" in card and "an admin set up" in card
    assert "server-api-token" not in card


def test_card_connected_shows_site_account_project_scopes_never_tokens(client, sqlite_engine):
    _save(sqlite_engine, project_key="OPS")
    card = _jira_card(client.get("/ui/connections").text)
    assert "Connected" in card and "Vraj Ved" in card
    assert "acme" in card and "https://acme.atlassian.net" in card
    assert "OPS" in card and "read:jira-work" in card
    assert "/ui/connections/jira/disconnect" in card
    assert "/ui/connections/jira/test" in card
    assert "Reconnect" in card
    assert ACCESS not in card and REFRESH not in card


@pytest.mark.parametrize(
    "name",
    [
        "HELIX_CONNECTIONS_KEY",
        "HELIX_JIRA_OAUTH_CLIENT_ID",
        "HELIX_JIRA_OAUTH_CLIENT_SECRET",
        "HELIX_SESSION_SECRET",
    ],
)
def test_connect_disabled_naming_the_missing_setting(client, monkeypatch, name):
    monkeypatch.delenv(name)
    card = _jira_card(client.get("/ui/connections").text)
    assert "disabled" in card.split("jira/connect")[1].split("</form>")[0]
    assert name in card
    refused = client.post("/ui/connections/jira/connect", follow_redirects=False)
    assert refused.status_code == 303
    assert "atlassian.com" not in refused.headers["location"]
    assert refused.headers["location"].startswith("/ui/connections?")


def test_connect_redirects_to_atlassian_with_params_and_state(client):
    response = client.post("/ui/connections/jira/connect", follow_redirects=False)
    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    query = parse_qs(target.query)
    assert (target.netloc, target.path) == ("auth.atlassian.com", "/authorize")
    assert query["audience"] == ["api.atlassian.com"]
    assert query["client_id"] == ["jira-cid"]
    assert query["response_type"] == ["code"]
    assert query["prompt"] == ["consent"]
    assert query["redirect_uri"] == [CALLBACK]
    scopes = query["scope"][0].split(" ")
    assert {"offline_access", "read:me", "read:jira-work", "write:jira-work"} <= set(scopes)
    assert len(query["state"][0]) >= 16
    assert "helix_connect_state" in response.headers["set-cookie"]


@pytest.mark.parametrize("query", ["code=abc&state=wrong", "code=abc", "state=x"])
def test_callback_with_bad_state_refuses_and_saves_nothing(client, atlassian, sqlite_engine, query):
    _start(client)
    response = client.get(f"/ui/connections/jira/callback?{query}", follow_redirects=False)
    assert response.status_code == 400
    assert "did not start" in response.text
    assert atlassian.calls == [] and _stored(sqlite_engine) is None


def test_callback_without_a_state_cookie_refuses(client, atlassian, sqlite_engine):
    response = client.get("/ui/connections/jira/callback?code=abc&state=x")
    assert response.status_code == 400
    assert atlassian.calls == [] and _stored(sqlite_engine) is None


def test_callback_success_saves_encrypted_connection(client, atlassian, sqlite_engine):
    response = _callback(client)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/connections?notice=")
    token_call = atlassian.calls[0]
    assert (token_call["method"], token_call["path"]) == ("POST", "/oauth/token")
    assert token_call["body"] == {
        "grant_type": "authorization_code",
        "client_id": "jira-cid",
        "client_secret": "jira-csecret",
        "code": "abc",
        "redirect_uri": CALLBACK,
    }
    resources = next(c for c in atlassian.calls if c["path"] == "/oauth/token/accessible-resources")
    assert resources["auth"] == f"Bearer {ACCESS}"
    before = datetime.now(UTC)
    stored = _stored(sqlite_engine)
    assert stored.account == "Vraj Ved"
    assert stored.access_token == ACCESS and stored.refresh_token == REFRESH
    expires = stored.expires_at.replace(tzinfo=stored.expires_at.tzinfo or UTC)
    assert timedelta(minutes=50) < expires - before < timedelta(minutes=70)
    assert "read:jira-work" in stored.scopes and "offline_access" in stored.scopes
    assert stored.settings == {
        "cloud_id": "cloud-1",
        "site_url": "https://acme.atlassian.net",
        "site_name": "acme",
    }
    page = client.get(response.headers["location"]).text
    assert "Connected Jira." in page and "Vraj Ved" in page
    assert ACCESS not in page and REFRESH not in page


def test_account_falls_back_to_the_site_when_me_fails(client, atlassian, sqlite_engine):
    atlassian.replies["/me"] = (500, {})
    _callback(client)
    assert _stored(sqlite_engine).account == "acme"


def test_site_without_jira_scopes_is_skipped_for_one_with(client, atlassian, sqlite_engine):
    confluence = {**SITE, "id": "conf-1", "scopes": ["read:confluence-content.all"]}
    atlassian.replies["/oauth/token/accessible-resources"] = (200, [confluence, SITE])
    _callback(client)
    assert _stored(sqlite_engine).settings["cloud_id"] == "cloud-1"


@pytest.mark.parametrize("sites", [[], [{**SITE, "scopes": ["read:confluence-content.all"]}]])
def test_no_jira_site_is_a_failure_and_saves_nothing(client, atlassian, sqlite_engine, sites):
    atlassian.replies["/oauth/token/accessible-resources"] = (200, sites)
    response = _callback(client)
    assert response.status_code == 303 and _stored(sqlite_engine) is None
    page = client.get(response.headers["location"]).text
    assert "no_jira_site" in page and "Nothing was saved" in page


def test_token_endpoint_error_is_a_failure_and_saves_nothing(client, atlassian, sqlite_engine):
    atlassian.replies["/oauth/token"] = (403, {"error": "invalid_grant"})
    response = _callback(client)
    assert response.status_code == 303 and _stored(sqlite_engine) is None
    assert "invalid_grant" in client.get(response.headers["location"]).text


def test_transport_failure_is_a_failure_and_saves_nothing(client, monkeypatch, sqlite_engine):
    def boom(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        jira_oauth, "_http", lambda: httpx.Client(transport=httpx.MockTransport(boom), timeout=5)
    )
    response = _callback(client)
    assert response.status_code == 303 and _stored(sqlite_engine) is None
    assert "transport_error" in client.get(response.headers["location"]).text


def test_callback_access_denied_is_handled(client, atlassian, sqlite_engine):
    state = _start(client)
    response = client.get(
        f"/ui/connections/jira/callback?error=access_denied&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert atlassian.calls == [] and _stored(sqlite_engine) is None
    assert "cancelled" in client.get(response.headers["location"]).text


def test_reconnecting_the_same_site_keeps_the_project_key(client, atlassian, sqlite_engine):
    _save(sqlite_engine, project_key="OPS")
    _callback(client)
    assert _stored(sqlite_engine).settings["project_key"] == "OPS"


def test_connecting_a_different_site_drops_the_project_key(client, atlassian, sqlite_engine):
    _save(sqlite_engine, project_key="OPS")
    atlassian.replies["/oauth/token/accessible-resources"] = (
        200,
        [{**SITE, "id": "cloud-2", "name": "other"}],
    )
    _callback(client)
    assert "project_key" not in _stored(sqlite_engine).settings


def test_test_button_reports_ok(client, atlassian, sqlite_engine):
    _save(sqlite_engine)
    response = client.post("/ui/connections/jira/test", follow_redirects=False)
    call = atlassian.calls[0]
    assert call["path"] == "/rest/api/3/myself" and call["auth"] == f"Bearer {ACCESS}"
    page = client.get(response.headers["location"]).text
    assert "Jira accepted the connection" in page
    assert ACCESS not in page


def test_test_button_refreshes_an_expired_token_and_saves_it(client, atlassian, sqlite_engine):
    _save(sqlite_engine, expires_at=datetime.now(UTC) - timedelta(minutes=5))
    atlassian.replies["/oauth/token"] = (
        200,
        {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600},
    )
    client.post("/ui/connections/jira/test", follow_redirects=False)
    assert atlassian.calls[0]["path"] == "/oauth/token"
    assert atlassian.calls[1]["auth"] == "Bearer new-access"
    assert _stored(sqlite_engine).refresh_token == "new-refresh"


def test_test_button_reports_a_rejected_connection(client, atlassian, sqlite_engine):
    _save(sqlite_engine)
    atlassian.replies["/rest/api/3/myself"] = (403, {})
    response = client.post("/ui/connections/jira/test", follow_redirects=False)
    page = client.get(response.headers["location"]).text
    assert "Jira rejected the connection" in page and "http_403" in page


def test_test_button_when_not_connected(client, atlassian):
    response = client.post("/ui/connections/jira/test", follow_redirects=False)
    assert atlassian.calls == []
    assert "Jira is not connected" in client.get(response.headers["location"]).text


def test_disconnect_deletes_without_calling_atlassian(client, atlassian, sqlite_engine):
    _save(sqlite_engine)
    response = client.post("/ui/connections/jira/disconnect", follow_redirects=False)
    assert response.status_code == 303
    assert _stored(sqlite_engine) is None and atlassian.calls == []
    assert "Disconnected Jira" in client.get(response.headers["location"]).text


def test_disconnect_when_not_connected_is_harmless(client, atlassian):
    response = client.post("/ui/connections/jira/disconnect", follow_redirects=False)
    assert response.status_code == 303


def test_everything_requires_sign_in(client, monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "VrajVed")
    for method, path in [
        ("post", "/ui/connections/jira/connect"),
        ("get", "/ui/connections/jira/callback?code=a&state=b"),
        ("post", "/ui/connections/jira/disconnect"),
        ("post", "/ui/connections/jira/test"),
    ]:
        response = getattr(client, method)(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].endswith("/login"), path


# -- the effective work provider ---------------------------------------


def test_connected_jira_makes_jira_the_effective_provider(sqlite_engine):
    _save(sqlite_engine)
    with Session(sqlite_engine) as session:
        assert effective_work_provider(session) == "jira"


def test_without_a_connection_the_provider_stays_memory(sqlite_engine):
    with Session(sqlite_engine) as session:
        assert effective_work_provider(session) == "memory"


def test_isolation_keeps_memory_even_with_a_connection(sqlite_engine, monkeypatch):
    _save(sqlite_engine)
    monkeypatch.setenv("HELIX_CONNECTIONS_ENABLED", "false")
    with Session(sqlite_engine) as session:
        assert effective_work_provider(session) == "memory"


@pytest.mark.parametrize("kind", ["linear", "trello", "jira"])
def test_an_explicit_provider_is_left_as_configured(sqlite_engine, monkeypatch, kind):
    _save(sqlite_engine)
    monkeypatch.setenv("HELIX_WORK_PROVIDER", kind)
    with Session(sqlite_engine) as session:
        assert effective_work_provider(session) == kind


def test_a_connection_without_a_site_does_not_switch_the_provider(sqlite_engine):
    with Session(sqlite_engine) as session:
        connections.save_connection(
            session,
            "jira",
            account="x",
            scopes=[],
            access_token="a",
            refresh_token="r",
            expires_at=None,
            settings={},
        )
        session.commit()
        assert effective_work_provider(session) == "memory"


def test_transitions_enqueue_jira_rows_for_a_connected_account(sqlite_engine):
    _save(sqlite_engine)
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
        enqueue_for_transition(session, incident, IncidentStatus.TRIAGED, cause="t")
        rows = session.query(models.IntegrationOutbox).all()
        assert [r.integration for r in rows if r.op == "create_work_item"] == ["jira"]
