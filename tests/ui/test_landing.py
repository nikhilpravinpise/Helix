"""The public landing page at "/" (hosted Helix) and the favicon.

Signed out with sign-in enabled: "/" is the landing page and nothing else
opens. Signed in, or with sign-in disabled (local development): "/" goes
straight to the dashboard."""

import time

import pytest

from helix import auth


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.delenv("HELIX_GITHUB_OAUTH_CLIENT_ID", raising=False)


@pytest.fixture
def oauth(monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("HELIX_SESSION_SECRET", "s3cret")
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "VrajVed")


def test_signed_out_visitor_gets_the_landing_page(client, oauth):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    assert body.count("<h1") == 1
    assert 'href="/auth/github"' in body
    assert '<meta name="description"' in body
    assert 'property="og:title"' in body
    assert 'class="app"' not in body and 'class="sidebar"' not in body


def test_landing_does_not_open_anything_else(client, oauth):
    page = client.get("/ui/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert client.get("/incidents").status_code == 401
    assert client.get("/ui/connections", follow_redirects=False).status_code == 303


def test_signed_in_visitor_goes_to_the_dashboard(client, oauth):
    cookie = auth._sign({"login": "VrajVed", "exp": time.time() + 600})
    client.cookies.set(auth.SESSION_COOKIE, cookie)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (303, 307) and resp.headers["location"] == "/ui/"


def test_an_expired_or_unlisted_session_still_sees_the_landing_page(client, oauth):
    stale = auth._sign({"login": "VrajVed", "exp": time.time() - 5})
    client.cookies.set(auth.SESSION_COOKIE, stale)
    assert client.get("/", follow_redirects=False).status_code == 200
    other = auth._sign({"login": "stranger", "exp": time.time() + 600})
    client.cookies.set(auth.SESSION_COOKIE, other)
    assert client.get("/", follow_redirects=False).status_code == 200


def test_with_sign_in_disabled_root_still_redirects_to_the_dashboard(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (303, 307) and resp.headers["location"] == "/ui/"


def test_favicon_is_served_and_linked_from_every_layout_page(client, oauth):
    icon = client.get("/static/favicon.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    for path in ("/login", "/"):
        assert (
            'rel="icon"' in client.get(path).text and "/static/favicon.svg" in client.get(path).text
        )
