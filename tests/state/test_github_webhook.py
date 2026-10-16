"""POST /webhooks/github: the GitHub App's signed webhook.

The signature is the whole authentication (GitHub cannot carry the intake
bearer token), so an unset secret closes the surface. Pull requests are
ingested and a run is scheduled after the response; check runs are only
ever recorded - on the hosted Helix every diagnosis reads one pinned
fixture, so a check-run incident must never be run. The runner is replaced
by a recorder; no request leaves the test.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from helix import ui
from helix.api import app, get_db_session
from state import models
from state.db import session_scope
from state.provenance import assemble_provenance

SECRET = "whsec-test"
REPO = "VrajVed/helix-sandbox"
SHA = "a" * 40

REGISTRY = """
services:
  payment-api:
    repo: VrajVed/helix-sandbox
    default_owner: VrajVed
    default_reviewers: [VrajVed, nikhilpravinpise]
  zz-twin:
    repo: vrajved/HELIX-SANDBOX
"""


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def pr_payload(action: str = "opened", repo: str = REPO, sha: str = SHA) -> dict:
    return {
        "action": action,
        "number": 7,
        "pull_request": {
            "title": "Bump the pool size",
            "user": {"login": "dana"},
            "head": {"ref": "feature/pool", "sha": sha},
            "base": {"ref": "main"},
            "created_at": "2026-10-16T10:00:00Z",
            "updated_at": "2026-10-16T10:00:00Z",
        },
        "repository": {"full_name": repo},
        "sender": {"login": "dana"},
    }


def check_payload(conclusion: str = "failure", action: str = "completed", sha: str = SHA) -> dict:
    return {
        "action": action,
        "check_run": {
            "name": "build",
            "status": "completed" if action == "completed" else "in_progress",
            "conclusion": conclusion if action == "completed" else None,
            "head_sha": sha,
            "html_url": f"https://github.com/{REPO}/runs/99",
            "started_at": "2026-10-16T10:01:00Z",
            "completed_at": "2026-10-16T10:02:00Z",
        },
        "repository": {"full_name": REPO},
    }


@pytest.fixture
def runs(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "agent.runner.run_until_parked", lambda incident_id: calls.append(incident_id)
    )
    return calls


@pytest.fixture
def client(sqlite_engine, tmp_path, monkeypatch, runs):
    registry = tmp_path / "registry.yaml"
    registry.write_text(REGISTRY)
    monkeypatch.setenv("HELIX_SERVICE_REGISTRY", str(registry))
    monkeypatch.setenv("HELIX_GITHUB_WEBHOOK_SECRET", SECRET)

    def override():
        with session_scope(sqlite_engine) as session:
            yield session

    app.dependency_overrides[get_db_session] = override
    app.dependency_overrides[ui.get_db_session] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def deliver(client, payload: dict, event: str, delivery: str = "d-1", signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": signature or sign(body),
        "Content-Type": "application/json",
    }
    return client.post("/webhooks/github", content=body, headers=headers)


def incidents(engine) -> list[models.Incident]:
    with session_scope(engine) as session:
        return list(session.scalars(select(models.Incident)))


class TestSignature:
    def test_unset_secret_closes_the_route(self, client, monkeypatch):
        monkeypatch.delenv("HELIX_GITHUB_WEBHOOK_SECRET")
        assert deliver(client, pr_payload(), "pull_request").status_code == 404

    def test_missing_signature_is_401(self, client):
        response = client.post(
            "/webhooks/github", content=b"{}", headers={"X-GitHub-Event": "ping"}
        )
        assert response.status_code == 401

    def test_wrong_signature_is_401(self, client, sqlite_engine, runs):
        bad = sign(json.dumps(pr_payload()).encode(), secret="other")
        assert deliver(client, pr_payload(), "pull_request", signature=bad).status_code == 401
        assert incidents(sqlite_engine) == []
        assert runs == []

    def test_signature_without_prefix_is_401(self, client):
        body = json.dumps(pr_payload()).encode()
        digest = sign(body).removeprefix("sha256=")
        assert deliver(client, pr_payload(), "pull_request", signature=digest).status_code == 401

    def test_ping(self, client):
        response = deliver(client, {"zen": "Keep it logically awesome."}, "ping")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "ping": True}


class TestPullRequest:
    def test_opened_creates_a_change_incident_and_schedules_one_run(
        self, client, sqlite_engine, runs
    ):
        response = deliver(client, pr_payload(), "pull_request")
        assert response.status_code == 200
        found = incidents(sqlite_engine)
        assert len(found) == 1
        assert found[0].kind == models.IncidentKind.CHANGE
        assert found[0].service == "payment-api"
        assert runs == [found[0].incident_id]

    def test_the_same_delivery_again_is_a_duplicate_and_runs_nothing_more(
        self, client, sqlite_engine, runs
    ):
        deliver(client, pr_payload(), "pull_request")
        again = deliver(client, pr_payload(), "pull_request")
        assert again.status_code == 200
        assert len(incidents(sqlite_engine)) == 1
        assert len(runs) == 1

    def test_closed_is_ignored(self, client, sqlite_engine, runs):
        response = deliver(client, pr_payload("closed"), "pull_request")
        assert response.status_code == 202
        assert response.json()["ok"] is True
        assert incidents(sqlite_engine) == []
        assert runs == []

    def test_unregistered_repo_is_ignored(self, client, sqlite_engine, runs):
        response = deliver(client, pr_payload(repo="someone/else"), "pull_request")
        assert response.status_code == 202
        assert response.json() == {"ok": True, "ignored": "repository not registered"}
        assert incidents(sqlite_engine) == []
        assert runs == []

    def test_repo_match_is_case_insensitive_and_first_service_by_name_wins(
        self, client, sqlite_engine
    ):
        deliver(client, pr_payload(repo="VRAJVED/Helix-Sandbox"), "pull_request")
        assert [i.service for i in incidents(sqlite_engine)] == ["payment-api"]

    def test_a_delivery_without_a_repository_is_ignored(self, client, runs):
        payload = pr_payload()
        del payload["repository"]
        assert deliver(client, payload, "pull_request").status_code == 202
        assert runs == []


class TestCheckRun:
    def test_failed_check_on_a_tracked_pr_correlates_and_never_runs(
        self, client, sqlite_engine, runs
    ):
        deliver(client, pr_payload(), "pull_request", delivery="d-pr")
        runs.clear()
        response = deliver(client, check_payload(), "check_run", delivery="d-check")
        assert response.status_code == 200
        found = incidents(sqlite_engine)
        assert len(found) == 1
        with session_scope(sqlite_engine) as session:
            types = [r.event_type for r in session.scalars(select(models.EventRecord))]
        assert "build.failed" in types
        assert runs == []

    def test_failed_check_with_no_incident_creates_one_that_is_never_run(
        self, client, sqlite_engine, runs
    ):
        response = deliver(client, check_payload(sha="b" * 40), "check_run")
        assert response.status_code == 200
        found = incidents(sqlite_engine)
        assert len(found) == 1
        assert runs == []
        said = (
            "Recorded from GitHub. Helix does not diagnose GitHub CI failures yet, "
            "because it cannot read live GitHub evidence here."
        )
        with session_scope(sqlite_engine) as session:
            record = assemble_provenance(session, found[0].incident_id)
        rows = {r["label"]: r["text"] for r in ui._summary(record)["rows"]}
        assert rows["What Helix did"] == said
        page = client.get(f"/ui/incidents/{found[0].incident_id}")
        assert page.status_code == 200
        assert said in page.text

    def test_successful_check_is_recorded_and_never_runs(self, client, runs):
        response = deliver(client, check_payload("success"), "check_run")
        assert response.status_code == 200
        assert runs == []

    def test_a_check_that_is_not_completed_is_ignored(self, client, sqlite_engine, runs):
        response = deliver(client, check_payload(action="created"), "check_run")
        assert response.status_code == 202
        assert incidents(sqlite_engine) == []
        assert runs == []

    def test_the_same_check_delivered_twice_is_recorded_once(self, client, sqlite_engine):
        deliver(client, check_payload(), "check_run", delivery="d-1")
        deliver(client, check_payload(), "check_run", delivery="d-2")
        with session_scope(sqlite_engine) as session:
            rows = session.scalars(
                select(models.EventRecord).where(models.EventRecord.event_type == "build.failed")
            )
            assert len(list(rows)) == 1


def test_other_events_are_ignored(client, sqlite_engine, runs):
    response = deliver(client, {"action": "created"}, "issues")
    assert response.status_code == 202
    assert response.json() == {"ok": True, "ignored": "issues"}
    assert incidents(sqlite_engine) == []
    assert runs == []


class TestSignInGate:
    @pytest.fixture(autouse=True)
    def sign_in_on(self, monkeypatch):
        monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
        monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("HELIX_SESSION_SECRET", "test-session-secret")

    def test_the_webhook_stays_open(self, client):
        assert deliver(client, {}, "ping").status_code == 200

    def test_nothing_else_became_public(self, client):
        assert client.get("/incidents").status_code == 401
        assert client.post("/webhooks/github/other", content=b"{}").status_code == 401
