"""HTTP contract tests for the intake/read API.

Unit tier: FastAPI's TestClient with the session dependency overridden to
an in-memory SQLite engine (conftest's `sqlite_engine`). The intake path
itself is the same `intake_signal()` used everywhere, so no stubbing is
needed. One integration test at the bottom exercises the unmodified app
against compose Postgres.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from helix.api import app, get_db_session
from state import models
from state.db import session_scope

K8S_SIGNAL = {
    "source": "kubernetes",
    "service": "payment-api",
    "environment": "sandbox",
    "signature": {"waiting_reason": "CrashLoopBackOff", "container": "payment-api"},
    "raw": {"pod": "payment-api-7c886558c8-f29pk"},
}

JENKINS_SIGNAL = {
    "source": "jenkins",
    "service": "payment-api",
    "signature": {"job": "payment-api", "stage": "Test"},
}


@pytest.fixture
def client(sqlite_engine):
    """TestClient whose DB sessions run on the per-test SQLite engine."""

    def override():
        with session_scope(sqlite_engine) as session:
            yield session

    app.dependency_overrides[get_db_session] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestIntakeEndpoints:
    def test_valid_kubernetes_signal_returns_incident_id(self, client):
        response = client.post("/intake/kubernetes", json=K8S_SIGNAL)
        assert response.status_code == 200
        body = response.json()
        assert body["incident_id"].startswith("INC-")
        assert body["created"] is True
        assert body["signal_id"] >= 1

    def test_repeat_signal_attaches_to_same_incident(self, client):
        first = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()
        second = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()
        assert second["incident_id"] == first["incident_id"]
        assert second["created"] is False

    def test_jenkins_endpoint(self, client):
        response = client.post("/intake/jenkins", json=JENKINS_SIGNAL)
        assert response.status_code == 200
        assert response.json()["created"] is True

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"source": "kubernetes"},  # missing service
            {"service": "payment-api"},  # missing source
            {"source": "pagerduty", "service": "payment-api"},
            {"source": "kubernetes", "service": "x", "environment": "qa"},
            {"source": "kubernetes", "service": "x", "signature": "nope"},
        ],
    )
    def test_malformed_payloads_return_422(self, client, payload):
        assert client.post("/intake/kubernetes", json=payload).status_code == 422

    def test_source_must_match_endpoint(self, client):
        response = client.post("/intake/kubernetes", json=JENKINS_SIGNAL)
        assert response.status_code == 422

    def test_not_json_returns_422(self, client):
        response = client.post(
            "/intake/kubernetes",
            content="not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422


class TestIntakeAuth:
    """HELIX_INTAKE_TOKEN gates intake POSTs when set (monkeypatched env;
    get_settings() is read fresh per request)."""

    def test_token_set_missing_header_401(self, client, monkeypatch):
        monkeypatch.setenv("HELIX_INTAKE_TOKEN", "test-token")
        response = client.post("/intake/kubernetes", json=K8S_SIGNAL)
        assert response.status_code == 401

    def test_token_set_wrong_token_403(self, client, monkeypatch):
        monkeypatch.setenv("HELIX_INTAKE_TOKEN", "test-token")
        response = client.post(
            "/intake/kubernetes",
            json=K8S_SIGNAL,
            headers={"Authorization": "Bearer wrong"},
        )
        assert response.status_code == 403

    def test_token_set_correct_token_passes(self, client, monkeypatch):
        monkeypatch.setenv("HELIX_INTAKE_TOKEN", "test-token")
        response = client.post(
            "/intake/kubernetes",
            json=K8S_SIGNAL,
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 200

    def test_token_unset_allows_posts(self, client, monkeypatch):
        # Empty string is the documented "unset" value; env override also
        # shields the test from a developer .env that sets the token.
        monkeypatch.setenv("HELIX_INTAKE_TOKEN", "")
        assert client.post("/intake/kubernetes", json=K8S_SIGNAL).status_code == 200


class TestIncidentReads:
    def test_list_incidents(self, client):
        client.post("/intake/kubernetes", json=K8S_SIGNAL)
        response = client.get("/incidents")
        assert response.status_code == 200
        incidents = response.json()
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident["incident_id"].startswith("INC-")
        assert incident["service"] == "payment-api"
        assert incident["status"] == "received"
        assert incident["signal_count"] == 1
        assert incident["attempt_count"] == 0

    def test_get_incident_detail(self, client):
        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        response = client.get(f"/incidents/{incident_id}")
        assert response.status_code == 200
        detail = response.json()
        assert detail["incident_id"] == incident_id
        assert len(detail["signals"]) == 1
        assert detail["signals"][0]["signature"]["waiting_reason"] == "CrashLoopBackOff"
        assert detail["signals"][0]["raw"]["pod"] == "payment-api-7c886558c8-f29pk"
        # One transition: the creation row (NULL -> received).
        assert len(detail["transitions"]) == 1
        assert detail["transitions"][0]["from_status"] is None
        assert detail["transitions"][0]["to_status"] == "received"

    def test_get_unknown_incident_404(self, client):
        assert client.get("/incidents/INC-nope0000").status_code == 404


@pytest.mark.integration
def test_intake_endpoint_against_real_postgres(pg):
    """The unmodified app (no dependency override) writes through the real
    session path to compose Postgres."""
    with TestClient(app) as test_client:
        response = test_client.post("/intake/kubernetes", json=K8S_SIGNAL)
        assert response.status_code == 200
        incident_id = response.json()["incident_id"]

    with session_scope(pg) as session:
        incident = session.scalar(
            select(models.Incident).where(models.Incident.incident_id == incident_id)
        )
        assert incident is not None
        assert len(incident.signals) == 1
