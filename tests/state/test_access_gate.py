"""A password in front of everything when Helix is on a public URL.

With HELIX_UI_PASSWORD set, every page and API route needs HTTP Basic
credentials (HELIX_UI_USER, default "helix"), or the intake token as a
Bearer header: the dashboard's own approve/reject calls and machine
clients send that, and both use the one Authorization header. Two routes
stay open: /healthz for the host's health check, and /slack/interactions,
which Slack signs and Helix verifies. Unset, nothing changes.
"""

import base64

import pytest
from fastapi.testclient import TestClient

from helix.api import app, get_db_session


@pytest.fixture
def client(sqlite_engine):
    from sqlalchemy.orm import Session

    def override():
        with Session(sqlite_engine) as session:
            yield session

    app.dependency_overrides[get_db_session] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_without_a_password_nothing_changes(client, monkeypatch):
    monkeypatch.delenv("HELIX_UI_PASSWORD", raising=False)
    assert client.get("/incidents").status_code == 200


def test_with_a_password_every_route_needs_it(client, monkeypatch):
    monkeypatch.setenv("HELIX_UI_PASSWORD", "s3cret")
    response = client.get("/incidents")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")
    assert client.get("/ui/").status_code == 401
    assert client.get("/incidents", headers=_basic("helix", "wrong")).status_code == 401
    assert client.get("/incidents", headers=_basic("helix", "s3cret")).status_code == 200


def test_the_user_name_is_configurable(client, monkeypatch):
    monkeypatch.setenv("HELIX_UI_PASSWORD", "s3cret")
    monkeypatch.setenv("HELIX_UI_USER", "vraj")
    assert client.get("/incidents", headers=_basic("helix", "s3cret")).status_code == 401
    assert client.get("/incidents", headers=_basic("vraj", "s3cret")).status_code == 200


def test_the_intake_token_also_gets_through(client, monkeypatch):
    monkeypatch.setenv("HELIX_UI_PASSWORD", "s3cret")
    monkeypatch.setenv("HELIX_INTAKE_TOKEN", "tok")
    bearer = {"Authorization": "Bearer tok"}
    assert client.get("/incidents", headers=bearer).status_code == 200
    assert client.get("/incidents", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_health_and_slack_stay_open(client, monkeypatch):
    monkeypatch.setenv("HELIX_UI_PASSWORD", "s3cret")
    health = client.get("/healthz")
    assert health.status_code == 200 and health.json() == {"ok": True}
    # Slack's own signature check answers, not the password gate.
    assert client.post("/slack/interactions", content=b"payload=%7B%7D").status_code != 401
