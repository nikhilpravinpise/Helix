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


class TestApprovalEndpoints:
    """The 1.8 approval surface: list pending/decided rows, decide, and
    resume. The resume call is monkeypatched - the unit contract is the
    durable decision, not the graph drive."""

    def _seed_pending(self, sqlite_engine, incident_id: str) -> int:
        """An approvals row as the coordinator would have parked it."""
        with session_scope(sqlite_engine) as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            row = models.Approval(
                incident_id=incident.id,
                action="reapply_manifest",
                params={"path": "sandbox/payment-api/k8s/03-secret.yaml"},
                fingerprint="fp-test",
                status=models.ApprovalStatus.PENDING,
            )
            session.add(row)
            session.flush()
            return row.id

    def test_list_approvals(self, client, sqlite_engine):
        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        approval_id = self._seed_pending(sqlite_engine, incident_id)
        response = client.get("/approvals")
        assert response.status_code == 200
        rows = response.json()
        assert len(rows) == 1
        assert rows[0]["id"] == approval_id
        assert rows[0]["incident_id"] == incident_id
        assert rows[0]["status"] == "pending"
        assert rows[0]["action"] == "reapply_manifest"

    def test_list_approvals_filters(self, client, sqlite_engine):
        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        self._seed_pending(sqlite_engine, incident_id)
        assert client.get("/approvals", params={"status": "pending"}).json()
        assert client.get("/approvals", params={"status": "approved"}).json() == []
        assert client.get("/approvals", params={"incident_id": "INC-nope"}).json() == []

    def test_approve_decides_and_resumes(self, client, sqlite_engine, monkeypatch):
        from agent.runner import RunResult

        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        approval_id = self._seed_pending(sqlite_engine, incident_id)
        calls = []

        def fake_run(iid, **kw):
            calls.append(iid)
            return RunResult(
                incident_id=iid,
                status="verifying",
                run_status=models.RunStatus.TERMINAL,
            )

        monkeypatch.setattr("agent.runner.run_incident", fake_run)
        # decided by an owner: only they may decide (state/deciders.py)
        response = client.post(f"/approvals/{approval_id}/approve", json={"actor": "alice"})

        assert response.status_code == 200
        body = response.json()
        assert body["approval"]["status"] == "approved"
        assert body["approval"]["decided_by"] == "alice"
        assert body["resumed"] is True
        assert body["status"] == "verifying"
        assert calls == [incident_id]

    def test_reject_decides_and_resumes(self, client, sqlite_engine, monkeypatch):
        from agent.runner import RunResult

        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        approval_id = self._seed_pending(sqlite_engine, incident_id)
        monkeypatch.setattr(
            "agent.runner.run_incident",
            lambda iid, **kw: RunResult(
                incident_id=iid,
                status="remediation_pending",
                run_status=models.RunStatus.TERMINAL,
            ),
        )
        # decided by an owner: only they may decide (state/deciders.py)
        response = client.post(
            f"/approvals/{approval_id}/reject",
            json={"actor": "alice", "note": "wait for the freeze"},
        )
        body = response.json()
        assert response.status_code == 200
        assert body["approval"]["status"] == "rejected"
        assert body["approval"]["note"] == "wait for the freeze"

    def test_unknown_approval_404(self, client):
        response = client.post("/approvals/9999/approve", json={"actor": "x"})
        assert response.status_code == 404

    def test_already_decided_409(self, client, sqlite_engine, monkeypatch):
        from agent.runner import RunResult

        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        approval_id = self._seed_pending(sqlite_engine, incident_id)
        monkeypatch.setattr(
            "agent.runner.run_incident",
            lambda iid, **kw: RunResult(
                incident_id=iid,
                status="verifying",
                run_status=models.RunStatus.TERMINAL,
            ),
        )
        # both actors are owners who would otherwise be allowed to decide
        # (state/deciders.py) - the 409 is about the row already being
        # decided, not about either identity.
        assert (
            client.post(f"/approvals/{approval_id}/approve", json={"actor": "alice"}).status_code
            == 200
        )
        second = client.post(f"/approvals/{approval_id}/reject", json={"actor": "bob"})
        assert second.status_code == 409

    def test_missing_actor_is_422(self, client, sqlite_engine):
        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        approval_id = self._seed_pending(sqlite_engine, incident_id)
        response = client.post(f"/approvals/{approval_id}/approve", json={})
        assert response.status_code == 422

    def test_approval_decisions_require_intake_token_when_set(
        self, client, sqlite_engine, monkeypatch
    ):
        """Decisions carry the same local-only trust as intake POSTs."""
        monkeypatch.setenv("HELIX_INTAKE_TOKEN", "test-token")
        incident_id = client.post(
            "/intake/kubernetes",
            json=K8S_SIGNAL,
            headers={"Authorization": "Bearer test-token"},
        ).json()["incident_id"]
        approval_id = self._seed_pending(sqlite_engine, incident_id)
        response = client.post(f"/approvals/{approval_id}/approve", json={"actor": "x"})
        assert response.status_code == 401


class TestReviewEndpoints:
    """The 2.6 review-gate surface: list review requests, record the
    reviewer's decision durably, then resume - same shape as approvals."""

    def _seed_review(self, sqlite_engine, incident_id: str) -> int:
        """A pending review_requests row as the review node parked it."""
        with session_scope(sqlite_engine) as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            row = models.ReviewRequest(
                incident_id=incident.id,
                reviewer="bob",
                rationale="owner for payment-api",
                status=models.ApprovalStatus.PENDING,
            )
            session.add(row)
            session.flush()
            return row.id

    def test_list_reviews(self, client, sqlite_engine):
        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        review_id = self._seed_review(sqlite_engine, incident_id)
        response = client.get("/reviews")
        assert response.status_code == 200
        rows = response.json()
        assert len(rows) == 1
        assert rows[0]["id"] == review_id
        assert rows[0]["incident_id"] == incident_id
        assert rows[0]["reviewer"] == "bob"
        assert rows[0]["status"] == "pending"

    def test_list_reviews_filters(self, client, sqlite_engine):
        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        self._seed_review(sqlite_engine, incident_id)
        assert client.get("/reviews", params={"status": "pending"}).json()
        assert client.get("/reviews", params={"status": "approved"}).json() == []
        assert client.get("/reviews", params={"incident_id": "INC-nope"}).json() == []

    def test_approve_decides_and_resumes(self, client, sqlite_engine, monkeypatch):
        from agent.runner import RunResult

        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        review_id = self._seed_review(sqlite_engine, incident_id)
        calls = []

        def fake_run(iid, **kw):
            calls.append(iid)
            return RunResult(
                incident_id=iid,
                status="approved",
                run_status=models.RunStatus.TERMINAL,
            )

        monkeypatch.setattr("agent.runner.run_incident", fake_run)
        response = client.post(f"/reviews/{review_id}/approve", json={"actor": "bob"})
        assert response.status_code == 200
        body = response.json()
        assert body["review"]["status"] == "approved"
        assert body["review"]["decided_by"] == "bob"
        assert body["resumed"] is True
        assert calls == [incident_id]

    def test_reject_decides_and_resumes(self, client, sqlite_engine, monkeypatch):
        from agent.runner import RunResult

        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        review_id = self._seed_review(sqlite_engine, incident_id)
        monkeypatch.setattr(
            "agent.runner.run_incident",
            lambda iid, **kw: RunResult(
                incident_id=iid,
                status="fix_required",
                run_status=models.RunStatus.TERMINAL,
            ),
        )
        response = client.post(
            f"/reviews/{review_id}/reject",
            json={"actor": "bob", "note": "changes requested"},
        )
        body = response.json()
        assert response.status_code == 200
        assert body["review"]["status"] == "rejected"
        assert body["review"]["note"] == "changes requested"

    def test_unknown_review_404(self, client):
        assert client.post("/reviews/9999/approve", json={"actor": "x"}).status_code == 404

    def test_already_decided_409(self, client, sqlite_engine, monkeypatch):
        from agent.runner import RunResult

        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        review_id = self._seed_review(sqlite_engine, incident_id)
        monkeypatch.setattr(
            "agent.runner.run_incident",
            lambda iid, **kw: RunResult(
                incident_id=iid,
                status="approved",
                run_status=models.RunStatus.TERMINAL,
            ),
        )
        # decided by the requested reviewer: only they may decide (state/deciders.py);
        # the 409 is about the row already being decided, not the identity.
        assert (
            client.post(f"/reviews/{review_id}/approve", json={"actor": "bob"}).status_code == 200
        )
        assert client.post(f"/reviews/{review_id}/reject", json={"actor": "bob"}).status_code == 409


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


class TestDecideInTheBackground:
    """`?wait=false` (the dashboard's buttons): the decision is durable at
    once, the run resumes after the response as a background task - the
    same multi-hop resume a Slack click gets - and the response is a 202
    with the incident's status at decision time. Without it nothing
    changes: the CLI and machine clients still get the resumed result."""

    @pytest.fixture
    def runs(self, monkeypatch):
        calls = {"until_parked": [], "single": []}

        def until_parked(iid, **kw):
            calls["until_parked"].append(iid)

        def single(iid, **kw):
            calls["single"].append(iid)
            raise AssertionError("wait=false must not resume inside the request")

        monkeypatch.setattr("agent.runner.run_until_parked", until_parked)
        monkeypatch.setattr("agent.runner.run_incident", single)
        return calls

    def test_approve_returns_at_once_and_resumes_after(self, client, sqlite_engine, runs):
        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        approval_id = TestApprovalEndpoints()._seed_pending(sqlite_engine, incident_id)
        before = client.get(f"/incidents/{incident_id}").json()["status"]

        response = client.post(
            f"/approvals/{approval_id}/approve?wait=false", json={"actor": "alice"}
        )

        assert response.status_code == 202
        body = response.json()
        assert body["approval"]["status"] == "approved"
        assert body["approval"]["decided_by"] == "alice"
        assert body["scheduled"] is True and body["resumed"] is False
        assert body["status"] == before
        assert runs == {"until_parked": [incident_id], "single": []}

    def test_reject_returns_at_once_and_resumes_after(self, client, sqlite_engine, runs):
        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        approval_id = TestApprovalEndpoints()._seed_pending(sqlite_engine, incident_id)

        response = client.post(
            f"/approvals/{approval_id}/reject?wait=false", json={"actor": "alice"}
        )

        assert response.status_code == 202
        assert response.json()["approval"]["status"] == "rejected"
        assert runs["until_parked"] == [incident_id]

    def test_a_failed_background_resume_keeps_the_decision(
        self, client, sqlite_engine, monkeypatch
    ):
        def boom(iid, **kw):
            raise RuntimeError("cluster unreachable")

        monkeypatch.setattr("agent.runner.run_until_parked", boom)
        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        approval_id = TestApprovalEndpoints()._seed_pending(sqlite_engine, incident_id)

        response = client.post(
            f"/approvals/{approval_id}/approve?wait=false", json={"actor": "alice"}
        )

        assert response.status_code == 202
        rows = client.get("/approvals", params={"incident_id": incident_id}).json()
        assert rows[0]["status"] == "approved"

    def test_an_already_decided_approval_is_still_a_409(self, client, sqlite_engine, runs):
        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        approval_id = TestApprovalEndpoints()._seed_pending(sqlite_engine, incident_id)
        url = f"/approvals/{approval_id}/approve?wait=false"
        assert client.post(url, json={"actor": "alice"}).status_code == 202
        assert client.post(url, json={"actor": "alice"}).status_code == 409
        assert runs["until_parked"] == [incident_id]

    def test_a_review_returns_at_once_and_resumes_after(self, client, sqlite_engine, runs):
        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        review_id = TestReviewEndpoints()._seed_review(sqlite_engine, incident_id)

        response = client.post(f"/reviews/{review_id}/approve?wait=false", json={"actor": "bob"})

        assert response.status_code == 202
        body = response.json()
        assert body["review"]["status"] == "approved"
        assert body["scheduled"] is True and body["resumed"] is False
        assert runs == {"until_parked": [incident_id], "single": []}

    def test_a_review_reject_returns_at_once(self, client, sqlite_engine, runs):
        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        review_id = TestReviewEndpoints()._seed_review(sqlite_engine, incident_id)

        response = client.post(f"/reviews/{review_id}/reject?wait=false", json={"actor": "bob"})

        assert response.status_code == 202
        assert response.json()["review"]["status"] == "rejected"
