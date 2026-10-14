"""Sign in with GitHub in front of a hosted Helix (decided by Vraj 2026-10-14).

With a GitHub OAuth app configured, every page needs a signed-in user on
the allow-list (HELIX_ALLOWED_USERS, default: the people in the service
registry); pages send a signed-out visitor to /login, API routes answer
401. The intake token as a Bearer header still gets machine clients
through. /healthz, /slack/interactions (Slack-signed) and the sign-in
routes stay open. Without an OAuth app configured nothing changes.
GitHub itself is faked here; no request leaves the test.
"""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from helix import auth
from helix.api import app, get_db_session


@pytest.fixture
def client(sqlite_engine, monkeypatch):
    from sqlalchemy.orm import Session

    def override():
        with Session(sqlite_engine) as session:
            yield session

    app.dependency_overrides[get_db_session] = override
    monkeypatch.setenv("HELIX_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("HELIX_PUBLIC_URL", "https://helix.example")
    with TestClient(app, base_url="https://helix.example") as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _enable(monkeypatch, login="VrajVed"):
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "csecret")
    # The test tier's registry is the benchmark cast, so name the list.
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "VrajVed,nikhilpravinpise")
    monkeypatch.setattr(auth, "github_login", lambda code, redirect_uri: login)


def _sign_in(client):
    start = client.get("/auth/github", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return client.get(f"/auth/github/callback?code=c&state={state}", follow_redirects=False)


def test_without_an_oauth_app_nothing_changes(client):
    assert client.get("/incidents").status_code == 200


def test_signed_out_pages_go_to_login_and_the_api_says_401(client, monkeypatch):
    _enable(monkeypatch)
    page = client.get("/ui/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"].endswith("/login")
    assert client.get("/incidents").status_code == 401
    assert "Sign in with GitHub" in client.get("/login").text


def test_github_redirect_asks_for_the_callback_and_a_state(client, monkeypatch):
    _enable(monkeypatch)
    start = client.get("/auth/github", follow_redirects=False)
    target = urlparse(start.headers["location"])
    query = parse_qs(target.query)
    assert target.netloc == "github.com"
    assert query["client_id"] == ["cid"]
    assert query["redirect_uri"] == ["https://helix.example/auth/github/callback"]
    assert query["state"][0]


def test_an_allowed_user_signs_in_and_sees_the_dashboard(client, monkeypatch):
    _enable(monkeypatch, login="VrajVed")
    done = _sign_in(client)
    assert done.status_code == 303 and done.headers["location"].endswith("/ui/")
    assert client.get("/incidents").status_code == 200
    page = client.get("/ui/")
    assert page.status_code == 200 and "VrajVed" in page.text and "Sign out" in page.text


def test_a_user_not_on_the_list_is_refused(client, monkeypatch):
    _enable(monkeypatch, login="mallory")
    refused = _sign_in(client)
    assert refused.status_code == 403 and "not allowed" in refused.text
    assert client.get("/incidents").status_code == 401


def test_a_forged_state_is_refused(client, monkeypatch):
    _enable(monkeypatch)
    client.get("/auth/github", follow_redirects=False)
    forged = client.get("/auth/github/callback?code=c&state=forged", follow_redirects=False)
    assert forged.status_code == 400
    assert client.get("/incidents").status_code == 401


def test_a_tampered_session_cookie_is_refused(client, monkeypatch):
    _enable(monkeypatch)
    _sign_in(client)
    value = client.cookies.get(auth.SESSION_COOKIE)
    client.cookies.set(auth.SESSION_COOKIE, value[:-2] + "xx")
    assert client.get("/incidents").status_code == 401


def test_sign_out_ends_the_session(client, monkeypatch):
    _enable(monkeypatch)
    _sign_in(client)
    client.get("/logout", follow_redirects=False)
    assert client.get("/incidents").status_code == 401


def test_machines_still_use_the_intake_token(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("HELIX_INTAKE_TOKEN", "tok")
    assert client.get("/incidents", headers={"Authorization": "Bearer tok"}).status_code == 200
    assert client.get("/incidents", headers={"Authorization": "Bearer no"}).status_code == 401


def test_health_and_slack_stay_open(client, monkeypatch):
    _enable(monkeypatch)
    assert client.get("/healthz").json() == {"ok": True}
    assert client.post("/slack/interactions", content=b"payload=%7B%7D").status_code != 401


def test_the_allow_list_defaults_to_the_registry_people(monkeypatch):
    monkeypatch.delenv("HELIX_ALLOWED_USERS", raising=False)
    monkeypatch.setenv("HELIX_SERVICE_REGISTRY", "config/service_registry.yaml")
    assert auth.allowed_users() == {"VrajVed", "nikhilpravinpise"}
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "VrajVed")
    assert auth.allowed_users() == {"VrajVed"}
