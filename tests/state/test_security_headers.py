"""Response hardening headers (issue #60 item 1): set on every response,
pages, JSON API and /healthz alike. HSTS only when the public URL is https."""

import pytest
from fastapi.testclient import TestClient

from helix import ui
from helix.api import app, get_db_session


@pytest.fixture
def client(sqlite_engine, monkeypatch):
    from state.db import session_scope

    def override():
        with session_scope(sqlite_engine) as session:
            yield session

    app.dependency_overrides[get_db_session] = override
    app.dependency_overrides[ui.get_db_session] = override
    monkeypatch.delenv("HELIX_GITHUB_OAUTH_CLIENT_ID", raising=False)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _has_base_headers(response) -> None:
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"


@pytest.mark.parametrize("path", ["/ui/", "/incidents", "/healthz"])
def test_headers_are_on_pages_the_api_and_healthz(client, monkeypatch, path):
    monkeypatch.setenv("HELIX_PUBLIC_URL", "http://helix.test")
    response = client.get(path)
    assert response.status_code == 200
    _has_base_headers(response)


def test_headers_are_on_a_refused_response_too(client, monkeypatch):
    monkeypatch.setenv("HELIX_PUBLIC_URL", "http://helix.test")
    _has_base_headers(client.get("/no-such-route"))


def test_hsts_is_sent_only_for_an_https_public_url(client, monkeypatch):
    monkeypatch.setenv("HELIX_PUBLIC_URL", "https://helix.example")
    assert client.get("/healthz").headers["strict-transport-security"] == "max-age=31536000"


def test_hsts_is_absent_when_not_https(client, monkeypatch):
    monkeypatch.setenv("HELIX_PUBLIC_URL", "http://helix.test")
    assert "strict-transport-security" not in client.get("/healthz").headers
