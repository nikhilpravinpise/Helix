"""Connections page and Connect Slack (future_plans.md Stage 1, slice 6).

Slack itself is faked with httpx.MockTransport behind
integrations.slack.oauth._http; no request leaves the test.
"""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy.orm import Session

from helix import auth
from integrations.slack import oauth
from state import connections

CALLBACK = "https://helix.example/ui/connections/slack/callback"
BOT_TOKEN = "xoxb-secret-bot-token"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setenv("HELIX_PUBLIC_URL", "https://helix.example")
    monkeypatch.setenv("HELIX_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "test connections key")
    monkeypatch.setenv("HELIX_SLACK_CLIENT_ID", "slack-cid")
    monkeypatch.setenv("HELIX_SLACK_CLIENT_SECRET", "slack-csecret")
    for name in ("HELIX_GITHUB_OAUTH_CLIENT_ID", "HELIX_SLACK_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def https(client):
    """The state cookie is Secure under the https public URL."""
    client.base_url = "https://helix.example"


@pytest.fixture
def slack(monkeypatch):
    """Fake Slack Web API: `replies` maps method -> JSON, `calls` records."""
    calls: list[tuple[str, dict]] = []
    replies: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        calls.append((method, {**form, "_auth": request.headers.get("authorization", "")}))
        return httpx.Response(200, json=replies.get(method, {"ok": True}))

    monkeypatch.setattr(
        oauth, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    )
    return type("Slack", (), {"calls": calls, "replies": replies})


OK_ACCESS = {
    "ok": True,
    "access_token": BOT_TOKEN,
    "scope": "chat:write,chat:write.public,channels:read",
    "bot_user_id": "U123",
    "team": {"id": "T1", "name": "Acme"},
}


def _start(client) -> str:
    response = client.post("/ui/connections/slack/connect", follow_redirects=False)
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


def _stored(sqlite_engine):
    with Session(sqlite_engine) as session:
        return connections.get_connection(session, "slack")


def _save(sqlite_engine, **settings):
    with Session(sqlite_engine) as session:
        connections.save_connection(
            session,
            "slack",
            account="Acme",
            scopes=["chat:write"],
            access_token=BOT_TOKEN,
            refresh_token=None,
            expires_at=None,
            settings={"team_id": "T1", **settings},
        )
        session.commit()


def test_page_not_connected_offers_connect(client):
    page = client.get("/ui/connections")
    assert page.status_code == 200
    assert "Slack" in page.text and "Not connected" in page.text
    assert 'action="/ui/connections/slack/connect"' in page.text
    assert "disabled" not in page.text.split("slack/connect")[1].split("</form>")[0]
    assert "/ui/connections" in client.get("/ui/").text


def test_page_not_connected_with_server_token_says_so(client, monkeypatch):
    monkeypatch.setenv("HELIX_SLACK_BOT_TOKEN", BOT_TOKEN)
    page = client.get("/ui/connections").text
    assert "Not connected" in page and "an admin set up" in page
    assert BOT_TOKEN not in page


def test_page_connected_shows_account_and_actions_never_the_token(client, sqlite_engine):
    _save(sqlite_engine)
    page = client.get("/ui/connections").text
    assert "Connected" in page and "Acme" in page
    assert "chat:write" in page
    assert "/ui/connections/slack/disconnect" in page
    assert "/ui/connections/slack/test" in page
    assert BOT_TOKEN not in page


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("HELIX_CONNECTIONS_KEY", "HELIX_CONNECTIONS_KEY"),
        ("HELIX_SLACK_CLIENT_ID", "HELIX_SLACK_CLIENT_ID"),
        ("HELIX_SLACK_CLIENT_SECRET", "HELIX_SLACK_CLIENT_SECRET"),
        ("HELIX_SESSION_SECRET", "HELIX_SESSION_SECRET"),
    ],
)
def test_connect_disabled_with_a_reason_when_unconfigured(client, monkeypatch, name, reason):
    monkeypatch.delenv(name)
    page = client.get("/ui/connections").text
    form = page.split("slack/connect")[1].split("</form>")[0]
    assert "disabled" in form
    assert reason in page
    refused = client.post("/ui/connections/slack/connect", follow_redirects=False)
    assert refused.status_code == 303
    assert "slack.com" not in refused.headers["location"]
    assert refused.headers["location"].startswith("/ui/connections?")


def test_connect_redirects_to_slack_with_params_and_state(client):
    response = client.post("/ui/connections/slack/connect", follow_redirects=False)
    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    query = parse_qs(target.query)
    assert (target.netloc, target.path) == ("slack.com", "/oauth/v2/authorize")
    assert query["client_id"] == ["slack-cid"]
    assert query["scope"] == ["chat:write,chat:write.public,channels:read"]
    assert query["redirect_uri"] == [CALLBACK]
    assert len(query["state"][0]) >= 16
    assert "helix_connect_state" in response.headers["set-cookie"]


def test_callback_success_saves_encrypted_connection_and_redirects(client, slack, sqlite_engine):
    slack.replies["oauth.v2.access"] = OK_ACCESS
    state = _start(client)
    response = client.get(
        f"/ui/connections/slack/callback?code=abc&state={state}", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/connections?notice=")
    method, form = slack.calls[0]
    assert method == "oauth.v2.access"
    assert form["code"] == "abc" and form["redirect_uri"] == CALLBACK
    assert form["client_id"] == "slack-cid" and form["client_secret"] == "slack-csecret"
    stored = _stored(sqlite_engine)
    assert stored.account == "Acme"
    assert stored.access_token == BOT_TOKEN and stored.refresh_token is None
    assert stored.expires_at is None
    assert stored.scopes == ["chat:write", "chat:write.public", "channels:read"]
    assert stored.settings == {"team_id": "T1", "team": "Acme", "bot_user_id": "U123"}
    page = client.get(response.headers["location"]).text
    assert "Connected Slack." in page and "Acme" in page


def test_reconnecting_the_same_team_keeps_the_channel(client, slack, sqlite_engine):
    _save(sqlite_engine, channel="#ops")
    slack.replies["oauth.v2.access"] = OK_ACCESS
    state = _start(client)
    client.get(f"/ui/connections/slack/callback?code=abc&state={state}", follow_redirects=False)
    assert _stored(sqlite_engine).settings["channel"] == "#ops"


def test_connecting_a_different_team_drops_the_channel(client, slack, sqlite_engine):
    _save(sqlite_engine, channel="#ops")
    slack.replies["oauth.v2.access"] = {**OK_ACCESS, "team": {"id": "T2", "name": "Other"}}
    state = _start(client)
    client.get(f"/ui/connections/slack/callback?code=abc&state={state}", follow_redirects=False)
    assert "channel" not in _stored(sqlite_engine).settings


@pytest.mark.parametrize("query", ["code=abc&state=wrong", "code=abc", "state=x"])
def test_callback_with_bad_state_refuses_and_saves_nothing(client, slack, sqlite_engine, query):
    _start(client)
    response = client.get(f"/ui/connections/slack/callback?{query}", follow_redirects=False)
    assert response.status_code == 400
    assert "did not start" in response.text
    assert slack.calls == []
    assert _stored(sqlite_engine) is None


def test_callback_without_a_state_cookie_refuses(client, slack, sqlite_engine):
    response = client.get("/ui/connections/slack/callback?code=abc&state=x")
    assert response.status_code == 400
    assert slack.calls == [] and _stored(sqlite_engine) is None


def test_callback_slack_ok_false_is_a_failure_notice(client, slack, sqlite_engine):
    slack.replies["oauth.v2.access"] = {"ok": False, "error": "invalid_code"}
    state = _start(client)
    response = client.get(
        f"/ui/connections/slack/callback?code=abc&state={state}", follow_redirects=False
    )
    assert response.status_code == 303
    assert _stored(sqlite_engine) is None
    page = client.get(response.headers["location"]).text
    assert "invalid_code" in page and "Not connected" in page


def test_callback_access_denied_is_handled(client, slack, sqlite_engine):
    state = _start(client)
    response = client.get(
        f"/ui/connections/slack/callback?error=access_denied&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert slack.calls == [] and _stored(sqlite_engine) is None
    assert "cancelled" in client.get(response.headers["location"]).text


def test_callback_transport_failure_is_a_failure_notice(client, monkeypatch, sqlite_engine):
    def boom(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        oauth, "_http", lambda: httpx.Client(transport=httpx.MockTransport(boom), timeout=5)
    )
    state = _start(client)
    response = client.get(
        f"/ui/connections/slack/callback?code=abc&state={state}", follow_redirects=False
    )
    assert response.status_code == 303 and _stored(sqlite_engine) is None


def test_disconnect_deletes_and_revokes_best_effort(client, slack, sqlite_engine):
    _save(sqlite_engine)
    slack.replies["auth.revoke"] = {"ok": False, "error": "token_revoked"}
    response = client.post("/ui/connections/slack/disconnect", follow_redirects=False)
    assert response.status_code == 303
    assert _stored(sqlite_engine) is None
    method, form = slack.calls[0]
    assert method == "auth.revoke" and BOT_TOKEN in form["_auth"]
    assert "Disconnected" in client.get(response.headers["location"]).text


def test_disconnect_survives_slack_being_down(client, monkeypatch, sqlite_engine):
    _save(sqlite_engine)

    def boom(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        oauth, "_http", lambda: httpx.Client(transport=httpx.MockTransport(boom), timeout=5)
    )
    client.post("/ui/connections/slack/disconnect", follow_redirects=False)
    assert _stored(sqlite_engine) is None


def test_disconnect_when_not_connected_is_harmless(client, slack):
    response = client.post("/ui/connections/slack/disconnect", follow_redirects=False)
    assert response.status_code == 303 and slack.calls == []


def test_test_button_reports_ok(client, slack, sqlite_engine):
    _save(sqlite_engine)
    slack.replies["auth.test"] = {"ok": True, "team": "Acme", "user": "helix"}
    response = client.post("/ui/connections/slack/test", follow_redirects=False)
    method, form = slack.calls[0]
    assert method == "auth.test" and BOT_TOKEN in form["_auth"]
    page = client.get(response.headers["location"]).text
    assert "Slack accepted the connection" in page
    assert BOT_TOKEN not in page


def test_test_button_reports_the_slack_error(client, slack, sqlite_engine):
    _save(sqlite_engine)
    slack.replies["auth.test"] = {"ok": False, "error": "token_revoked"}
    response = client.post("/ui/connections/slack/test", follow_redirects=False)
    assert "token_revoked" in client.get(response.headers["location"]).text


def test_test_button_when_not_connected(client, slack):
    response = client.post("/ui/connections/slack/test", follow_redirects=False)
    assert slack.calls == []
    assert "Not connected" in client.get(response.headers["location"]).text


def test_notice_text_is_not_reflected_from_the_query(client):
    page = client.get("/ui/connections?notice=<script>alert(1)</script>&detail=<b>x</b>").text
    assert "<script>alert" not in page and "<b>x</b>" not in page


@pytest.fixture
def signed_in(client, monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "VrajVed,nikhilpravinpise")
    monkeypatch.setattr(auth, "github_login", lambda code, redirect_uri: auth.GitHubUser("VrajVed"))
    start = client.get("/auth/github", follow_redirects=False)
    gh_state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    client.get(f"/auth/github/callback?code=c&state={gh_state}", follow_redirects=False)
    return client


def test_everything_requires_sign_in(client, monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "VrajVed")
    for method, path in [
        ("get", "/ui/connections"),
        ("post", "/ui/connections/slack/connect"),
        ("get", "/ui/connections/slack/callback?code=a&state=b"),
        ("post", "/ui/connections/slack/disconnect"),
        ("post", "/ui/connections/slack/test"),
    ]:
        response = getattr(client, method)(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].endswith("/login"), path


def test_signed_in_flow_works_and_state_is_bound_to_the_user(signed_in, slack, sqlite_engine):
    slack.replies["oauth.v2.access"] = OK_ACCESS
    state = _start(signed_in)
    response = signed_in.get(
        f"/ui/connections/slack/callback?code=abc&state={state}", follow_redirects=False
    )
    assert response.status_code == 303 and _stored(sqlite_engine).account == "Acme"


def test_state_started_by_another_user_is_refused(signed_in, slack, sqlite_engine, monkeypatch):
    _start(signed_in)
    forged = auth._sign(
        {"state": "s", "provider": "slack", "user": "nikhilpravinpise", "exp": 9999999999}
    )
    signed_in.cookies.set("helix_connect_state", forged, domain="helix.example")
    response = signed_in.get(
        "/ui/connections/slack/callback?code=abc&state=s", follow_redirects=False
    )
    assert response.status_code == 400
    assert slack.calls == [] and _stored(sqlite_engine) is None
