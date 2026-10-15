"""Connect GitHub on the Connections page (future_plans.md Stage 1, slice 8).

GitHub is faked with httpx.MockTransport behind
integrations.github.oauth._http; no request leaves the test. Connect reuses
the sign-in OAuth App, so these tests run signed in, as the page does.
"""

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy.orm import Session

from helix import auth
from integrations.github import oauth
from state import connections

CALLBACK_PATH = "/auth/github/callback/connect"
CALLBACK = f"https://helix.example{CALLBACK_PATH}"
TOKEN = "gho_secret_connect_token"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setenv("HELIX_PUBLIC_URL", "https://helix.example")
    monkeypatch.setenv("HELIX_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "test connections key")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "gh-cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "gh-csecret")
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "VrajVed,nikhilpravinpise")
    for name in ("HELIX_GITHUB_TOKEN", "HELIX_CONNECTIONS_ENABLED"):
        monkeypatch.delenv(name, raising=False)


def _sign_in(client, login="VrajVed"):
    client.cookies.clear()
    client.cookies.set(
        auth.SESSION_COOKIE,
        auth._sign({"login": login, "exp": time.time() + 3600}),
        domain="helix.example",
    )


@pytest.fixture(autouse=True)
def https(client):
    """The state cookie is Secure under the https public URL."""
    client.base_url = "https://helix.example"
    _sign_in(client)


@pytest.fixture
def github(monkeypatch):
    """Fake GitHub: `replies` maps (method, path) to (status, JSON)."""
    calls: list[dict] = []
    replies: dict[tuple[str, str], tuple[int, object]] = {
        ("POST", "/login/oauth/access_token"): (
            200,
            {"access_token": TOKEN, "scope": "repo", "token_type": "bearer"},
        ),
        ("GET", "/user"): (200, {"login": "VrajVed"}),
        ("DELETE", "/applications/gh-cid/grant"): (204, None),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        kind = request.headers.get("content-type", "")
        is_json = bool(request.content) and "json" in kind
        form = bool(request.content) and "form" in kind
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "auth": request.headers.get("authorization", ""),
                "body": json.loads(request.content) if is_json else None,
                "form": dict(parse_qs(request.content.decode())) if form else None,
            }
        )
        status, body = replies.get((request.method, request.url.path), (404, {}))
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    monkeypatch.setattr(
        oauth, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    )
    return type("GitHub", (), {"calls": calls, "replies": replies})


def _start(client) -> str:
    response = client.post("/ui/connections/github/connect", follow_redirects=False)
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


def _callback(client, state=None, query="code=abc"):
    state = state or _start(client)
    return client.get(f"{CALLBACK_PATH}?{query}&state={state}", follow_redirects=False)


def _stored(sqlite_engine):
    with Session(sqlite_engine) as session:
        return connections.get_connection(session, "github")


def _save(sqlite_engine):
    with Session(sqlite_engine) as session:
        connections.save_connection(
            session,
            "github",
            account="VrajVed",
            scopes=["repo"],
            access_token=TOKEN,
            refresh_token=None,
            expires_at=None,
            settings={},
        )
        session.commit()


def _card(page: str) -> str:
    return page.split('id="github"')[1].split("</section>")[0]


def test_card_not_connected_offers_connect(client):
    card = _card(client.get("/ui/connections").text)
    assert "GitHub" in card and "Not connected" in card
    assert 'action="/ui/connections/github/connect"' in card
    assert "disabled" not in card.split("github/connect")[1].split("</form>")[0]


def test_card_not_connected_with_server_token_says_so(client, monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_TOKEN", "server-github-token")
    card = _card(client.get("/ui/connections").text)
    assert "Not connected" in card and "server" in card.lower()
    assert "server-github-token" not in card


def test_card_connected_shows_login_and_scopes_never_the_token(client, sqlite_engine):
    _save(sqlite_engine)
    card = _card(client.get("/ui/connections").text)
    assert "Connected" in card and "VrajVed" in card and "repo" in card
    assert "/ui/connections/github/disconnect" in card
    assert "/ui/connections/github/test" in card
    assert "Reconnect" in card
    assert TOKEN not in card


@pytest.mark.parametrize(
    "name",
    [
        "HELIX_CONNECTIONS_KEY",
        "HELIX_GITHUB_OAUTH_CLIENT_SECRET",
    ],
)
def test_connect_disabled_naming_the_missing_setting(client, monkeypatch, name):
    monkeypatch.delenv(name)
    card = _card(client.get("/ui/connections").text)
    assert "disabled" in card.split("github/connect")[1].split("</form>")[0]
    assert name in card
    refused = client.post("/ui/connections/github/connect", follow_redirects=False)
    assert refused.status_code == 303
    assert "github.com" not in refused.headers["location"]
    assert refused.headers["location"].startswith("/ui/connections?")


def test_connect_redirects_to_github_with_repo_scope_and_state(client):
    response = client.post("/ui/connections/github/connect", follow_redirects=False)
    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    query = parse_qs(target.query)
    assert (target.netloc, target.path) == ("github.com", "/login/oauth/authorize")
    assert query["client_id"] == ["gh-cid"]
    assert query["scope"] == ["repo"]
    assert query["redirect_uri"] == [CALLBACK]
    assert len(query["state"][0]) >= 16
    assert "helix_connect_state" in response.headers["set-cookie"]


def test_redirect_uri_is_inside_the_registered_sign_in_callback(client):
    """A GitHub OAuth App has one registered callback URL; a redirect_uri is
    accepted only on the same host with that path or a subdirectory of it."""
    sign_in = client.get("/auth/github", follow_redirects=False)
    registered = urlparse(parse_qs(urlparse(sign_in.headers["location"]).query)["redirect_uri"][0])
    connect = client.post("/ui/connections/github/connect", follow_redirects=False)
    sent = urlparse(parse_qs(urlparse(connect.headers["location"]).query)["redirect_uri"][0])
    assert (sent.scheme, sent.netloc) == (registered.scheme, registered.netloc)
    assert sent.path.startswith(registered.path.rstrip("/") + "/")


def test_connect_callback_is_not_an_open_sign_in_path():
    assert CALLBACK_PATH not in auth._OPEN_PATHS


def test_callback_requires_sign_in_and_saves_nothing(client, github, sqlite_engine):
    state = _start(client)
    client.cookies.delete(auth.SESSION_COOKIE, domain="helix.example")
    response = client.get(f"{CALLBACK_PATH}?code=abc&state={state}", follow_redirects=False)
    assert response.status_code == 401
    assert github.calls == []
    assert _stored(sqlite_engine) is None


@pytest.mark.parametrize("query", ["code=abc&state=wrong", "code=abc", "state=x"])
def test_callback_with_bad_state_refuses_and_saves_nothing(client, github, sqlite_engine, query):
    _start(client)
    response = client.get(f"{CALLBACK_PATH}?{query}", follow_redirects=False)
    assert response.status_code == 400
    assert github.calls == []
    assert _stored(sqlite_engine) is None


def test_callback_for_a_state_another_user_started_is_refused(client, github, sqlite_engine):
    state = _start(client)
    cookie = client.cookies.get("helix_connect_state", domain="helix.example")
    _sign_in(client, "nikhilpravinpise")
    client.cookies.set("helix_connect_state", cookie, domain="helix.example")
    response = client.get(f"{CALLBACK_PATH}?code=abc&state={state}", follow_redirects=False)
    assert response.status_code == 400
    assert github.calls == []
    assert _stored(sqlite_engine) is None


def test_callback_success_saves_the_encrypted_connection_with_the_login(
    client, github, sqlite_engine
):
    response = _callback(client)
    assert response.status_code == 303
    assert "notice=github-connected" in response.headers["location"]
    exchange = next(c for c in github.calls if c["path"] == "/login/oauth/access_token")
    form = exchange["form"]
    assert form["code"] == ["abc"] and form["client_id"] == ["gh-cid"]
    assert form["client_secret"] == ["gh-csecret"] and form["redirect_uri"] == [CALLBACK]
    who = next(c for c in github.calls if c["path"] == "/user")
    assert who["auth"] == f"Bearer {TOKEN}"
    stored = _stored(sqlite_engine)
    assert stored is not None
    assert (stored.account, stored.scopes, stored.access_token) == ("VrajVed", ["repo"], TOKEN)
    assert (stored.refresh_token, stored.expires_at, stored.settings) == (None, None, {})
    from state.models import Connection

    with Session(sqlite_engine) as session:
        assert TOKEN not in session.get(Connection, "github").access_token
    assert "helix_connect_state" in response.headers["set-cookie"]


def test_callback_error_reply_shows_failure_and_saves_nothing(client, github, sqlite_engine):
    github.replies[("POST", "/login/oauth/access_token")] = (
        200,
        {"error": "bad_verification_code", "error_description": "The code is incorrect."},
    )
    response = _callback(client)
    assert "notice=github-failed&detail=bad_verification_code" in response.headers["location"]
    assert _stored(sqlite_engine) is None
    page = client.get(response.headers["location"]).text
    assert "bad_verification_code" in page and "Nothing was saved" in page


def test_callback_user_lookup_failure_saves_nothing(client, github, sqlite_engine):
    github.replies[("GET", "/user")] = (401, {"message": "Bad credentials"})
    response = _callback(client)
    assert "notice=github-failed" in response.headers["location"]
    assert _stored(sqlite_engine) is None


def test_callback_denied_by_user_saves_nothing(client, github, sqlite_engine):
    response = _callback(client, query="error=access_denied")
    assert "notice=github-denied" in response.headers["location"]
    assert github.calls == [] and _stored(sqlite_engine) is None


def test_callback_without_a_key_saves_nothing(client, github, sqlite_engine, monkeypatch):
    state = _start(client)
    monkeypatch.delenv("HELIX_CONNECTIONS_KEY")
    response = client.get(f"{CALLBACK_PATH}?code=abc&state={state}", follow_redirects=False)
    assert "notice=github-unconfigured" in response.headers["location"]
    assert _stored(sqlite_engine) is None


def test_test_button_ok(client, github, sqlite_engine):
    _save(sqlite_engine)
    response = client.post("/ui/connections/github/test", follow_redirects=False)
    assert "notice=github-test-ok" in response.headers["location"]
    assert github.calls[-1]["auth"] == f"Bearer {TOKEN}"


def test_test_button_reports_a_401(client, github, sqlite_engine):
    _save(sqlite_engine)
    github.replies[("GET", "/user")] = (401, {"message": "Bad credentials"})
    response = client.post("/ui/connections/github/test", follow_redirects=False)
    assert "notice=github-test-failed&detail=http_401" in response.headers["location"]
    assert _stored(sqlite_engine) is not None


def test_test_button_when_not_connected(client, github):
    response = client.post("/ui/connections/github/test", follow_redirects=False)
    assert "notice=github-not-connected" in response.headers["location"]
    assert github.calls == []


def test_disconnect_revokes_the_grant_and_deletes_the_row(client, github, sqlite_engine):
    _save(sqlite_engine)
    response = client.post("/ui/connections/github/disconnect", follow_redirects=False)
    assert "notice=github-disconnected" in response.headers["location"]
    revoke = next(c for c in github.calls if c["method"] == "DELETE")
    assert revoke["path"] == "/applications/gh-cid/grant"
    assert revoke["body"] == {"access_token": TOKEN}
    assert revoke["auth"].startswith("Basic ")
    assert _stored(sqlite_engine) is None


def test_disconnect_still_deletes_when_the_revoke_fails(client, github, sqlite_engine):
    _save(sqlite_engine)
    github.replies[("DELETE", "/applications/gh-cid/grant")] = (500, {})
    response = client.post("/ui/connections/github/disconnect", follow_redirects=False)
    assert "notice=github-disconnected" in response.headers["location"]
    assert _stored(sqlite_engine) is None


def test_disconnect_when_not_connected_is_harmless(client, github):
    response = client.post("/ui/connections/github/disconnect", follow_redirects=False)
    assert "notice=github-disconnected" in response.headers["location"]
    assert github.calls == []
