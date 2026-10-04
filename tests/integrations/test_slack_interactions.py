"""POST /slack/interactions contract tests (subphase 2.2).

Unit tier: FastAPI's TestClient with `get_db_session` overridden to a
per-test in-memory SQLite (the tests/state conftest pattern, replicated
locally). Payloads are signed with a real HMAC over the exact bytes sent,
so verification runs the same code Slack's signature exercises; the run
resume is monkeypatched - the unit contract is the durable decision.
"""

import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.pool import StaticPool

from helix.api import app, get_db_session
from state import models
from state.db import session_scope

SECRET = "slack-test-secret"

K8S_SIGNAL = {
    "source": "kubernetes",
    "service": "payment-api",
    "environment": "sandbox",
    "signature": {"waiting_reason": "CrashLoopBackOff", "container": "payment-api"},
    "raw": {"pod": "payment-api-7c886558c8-f29pk"},
}


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """Fresh in-memory SQLite per test (StaticPool shares one connection)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


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


@pytest.fixture(autouse=True)
def _slack_secret(monkeypatch):
    """Every test runs with a configured secret unless it overrides."""
    monkeypatch.setenv("HELIX_SLACK_SIGNING_SECRET", SECRET)


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return f"v0={digest}"


def _post(
    client: TestClient,
    payload: dict,
    *,
    secret: str = SECRET,
    timestamp: str | None = None,
) -> httpx.Response:
    """Form-encode `payload` the way Slack's interactive POSTs do and sign
    the exact body bytes."""
    ts = timestamp or str(int(time.time()))
    body = urlencode({"payload": json.dumps(payload)}).encode()
    return client.post(
        "/slack/interactions",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": _sign(secret, ts, body),
        },
    )


def _block_actions(approval_id: int, kind: str = "approve", **user) -> dict:
    return {
        "type": "block_actions",
        "user": {"username": "slack-carol", "id": "U123", **user},
        "actions": [{"action_id": f"helix_{kind}:{approval_id}", "value": str(approval_id)}],
    }


def _seed_incident(client: TestClient) -> str:
    return client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]


def _seed_pending(engine: Engine, incident_id: str, **overrides) -> int:
    """An approvals row as the coordinator would have parked it."""
    with session_scope(engine) as session:
        incident = session.scalar(
            select(models.Incident).where(models.Incident.incident_id == incident_id)
        )
        row = models.Approval(
            incident_id=incident.id,
            action="reapply_manifest",
            params={"path": "sandbox/payment-api/k8s/03-secret.yaml"},
            fingerprint="fp-test",
            status=models.ApprovalStatus.PENDING,
            **overrides,
        )
        session.add(row)
        session.flush()
        return row.id


def _get_approval(engine: Engine, approval_id: int) -> models.Approval:
    with session_scope(engine) as session:
        return session.get(models.Approval, approval_id)


def _stub_run(monkeypatch, calls: list[str], status: str = "recovered") -> None:
    """A run that ends where a real one stops. The default was `executing`,
    which no real run stops at; once a decision resumes with
    run_until_parked, which keeps going while the incident advances, a
    stub frozen at a non-stopping status read as one extra run."""
    from agent.runner import RunResult

    def fake_run(iid, **kw):
        calls.append(iid)
        return RunResult(incident_id=iid, status=status, run_status=models.RunStatus.TERMINAL)

    monkeypatch.setattr("agent.runner.run_incident", fake_run)


class TestBlockActions:
    def test_approve_records_decision_and_resumes(self, client, sqlite_engine, monkeypatch):
        incident_id = _seed_incident(client)
        approval_id = _seed_pending(sqlite_engine, incident_id)
        calls: list[str] = []
        _stub_run(monkeypatch, calls)

        response = _post(client, _block_actions(approval_id))

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["decisions"] == [
            {"approval_id": approval_id, "decision": "approved", "resumed": "scheduled"}
        ]
        assert calls == [incident_id]
        row = _get_approval(sqlite_engine, approval_id)
        assert row.status == models.ApprovalStatus.APPROVED
        assert row.decided_by == "slack-carol"

    def test_reject_records_decision_and_resumes(self, client, sqlite_engine, monkeypatch):
        incident_id = _seed_incident(client)
        approval_id = _seed_pending(sqlite_engine, incident_id)
        calls: list[str] = []
        _stub_run(monkeypatch, calls)

        response = _post(client, _block_actions(approval_id, kind="reject"))

        assert response.status_code == 200
        assert response.json()["decisions"][0]["decision"] == "rejected"
        assert calls == [incident_id]
        assert _get_approval(sqlite_engine, approval_id).status == (models.ApprovalStatus.REJECTED)

    def test_actor_falls_back_to_name_then_id(self, client, sqlite_engine, monkeypatch):
        incident_id = _seed_incident(client)
        approval_id = _seed_pending(sqlite_engine, incident_id)
        _stub_run(monkeypatch, [])
        payload = _block_actions(approval_id)
        payload["user"] = {"id": "U999", "name": "carol-name"}
        response = _post(client, payload)
        assert response.status_code == 200
        assert _get_approval(sqlite_engine, approval_id).decided_by == "carol-name"

    def test_duplicate_decision_is_200_with_note(self, client, sqlite_engine, monkeypatch):
        """Slack retries deliveries - a redelivered press must ack, not error."""
        incident_id = _seed_incident(client)
        approval_id = _seed_pending(sqlite_engine, incident_id)
        calls: list[str] = []
        _stub_run(monkeypatch, calls)

        first = _post(client, _block_actions(approval_id))
        second = _post(client, _block_actions(approval_id, kind="reject"))

        assert first.status_code == 200 and second.status_code == 200
        note = second.json()["decisions"][0].get("note", "")
        assert "already" in note
        assert calls == [incident_id]  # no second resume
        assert _get_approval(sqlite_engine, approval_id).status == (models.ApprovalStatus.APPROVED)

    def test_unknown_approval_is_200_with_note(self, client, monkeypatch):
        calls: list[str] = []
        _stub_run(monkeypatch, calls)
        response = _post(client, _block_actions(9999))
        assert response.status_code == 200
        assert "no such approval" in response.json()["decisions"][0]["note"]
        assert calls == []

    def test_unrecognized_action_id_is_200_with_note(self, client, monkeypatch):
        _stub_run(monkeypatch, [])
        payload = _block_actions(1)
        payload["actions"] = [{"action_id": "someone_elses:1", "value": "1"}]
        response = _post(client, payload)
        assert response.status_code == 200
        assert response.json()["decisions"][0]["note"] == "unrecognized action"

    def test_timed_out_approval_is_200_with_note(self, client, sqlite_engine, monkeypatch):
        """A decision arriving after HELIX_SLACK_APPROVAL_TIMEOUT is refused
        but still acked - the sweep owns the escalation, not the webhook."""
        monkeypatch.setenv("HELIX_SLACK_APPROVAL_TIMEOUT", "1")
        incident_id = _seed_incident(client)
        approval_id = _seed_pending(
            sqlite_engine,
            incident_id,
            created_at=datetime.now(UTC) - timedelta(hours=2),
        )
        calls: list[str] = []
        _stub_run(monkeypatch, calls)

        response = _post(client, _block_actions(approval_id))

        assert response.status_code == 200
        assert "timed out" in response.json()["decisions"][0]["note"]
        assert calls == []
        assert _get_approval(sqlite_engine, approval_id).status == (models.ApprovalStatus.PENDING)

    def test_resume_failure_still_acks(self, client, sqlite_engine, monkeypatch):
        """The decision is durable even when the resume raises - Slack must
        get its ack; `helix run` can drive the incident later."""
        incident_id = _seed_incident(client)
        approval_id = _seed_pending(sqlite_engine, incident_id)

        calls: list[str] = []

        def boom(iid, **kw):
            calls.append(iid)
            raise RuntimeError("graph blew up")

        monkeypatch.setattr("agent.runner.run_incident", boom)
        response = _post(client, _block_actions(approval_id))

        assert response.status_code == 200
        decision = response.json()["decisions"][0]
        assert decision["decision"] == "approved"
        # The resume is a background task now - the ack never waits on a
        # run, and a run failure lands in the log, not this response.
        # TestClient still executes the task (the wrapper swallows the
        # error), so the fake ran but the response only ever says
        # "scheduled".
        assert decision["resumed"] == "scheduled"
        assert calls == [incident_id]
        assert _get_approval(sqlite_engine, approval_id).status == (models.ApprovalStatus.APPROVED)


class TestVerification:
    def test_bad_signature_401(self, client):
        response = _post(client, {"type": "block_actions"}, secret="wrong-secret")
        assert response.status_code == 401

    def test_missing_signature_401(self, client):
        ts = str(int(time.time()))
        body = urlencode({"payload": json.dumps({"type": "block_actions"})}).encode()
        response = client.post(
            "/slack/interactions",
            content=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Slack-Request-Timestamp": ts,
            },
        )
        assert response.status_code == 401

    def test_stale_timestamp_401(self, client):
        response = _post(
            client,
            {"type": "block_actions"},
            timestamp=str(int(time.time()) - 1000),
        )
        assert response.status_code == 401

    def test_unset_secret_503(self, client, monkeypatch):
        monkeypatch.setenv("HELIX_SLACK_SIGNING_SECRET", "")
        response = _post(client, {"type": "block_actions"})
        assert response.status_code == 503

    def test_url_verification_returns_challenge(self, client):
        """Slack's endpoint handshake POSTs raw JSON, not a form field."""
        ts = str(int(time.time()))
        body = json.dumps({"type": "url_verification", "challenge": "ch-123"}).encode()
        response = client.post(
            "/slack/interactions",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": _sign(SECRET, ts, body),
            },
        )
        assert response.status_code == 200
        assert response.json() == {"challenge": "ch-123"}

    def test_other_payload_types_ignored(self, client):
        response = _post(client, {"type": "shortcut"})
        assert response.status_code == 200
        assert response.json() == {"ok": True, "ignored": "shortcut"}


def _seed_review(engine: Engine, incident_id: str) -> int:
    """A pending review_requests row as the review node parked it."""
    with session_scope(engine) as session:
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


def _get_review(engine: Engine, review_id: int) -> models.ReviewRequest:
    with session_scope(engine) as session:
        return session.get(models.ReviewRequest, review_id)


def _review_block_actions(review_id: int, kind: str = "approve", **user) -> dict:
    return {
        "type": "block_actions",
        "user": {"username": "slack-bob", "id": "U456", **user},
        "actions": [{"action_id": f"helix_review_{kind}:{review_id}", "value": str(review_id)}],
    }


class TestReviewBlockActions:
    """2.6's interactive half: the reviewer's Approve/Request-changes
    buttons land on the durable review_requests row, same contract as the
    approval gate."""

    def test_review_approve_records_decision_and_resumes(self, client, sqlite_engine, monkeypatch):
        incident_id = _seed_incident(client)
        review_id = _seed_review(sqlite_engine, incident_id)
        calls: list[str] = []
        _stub_run(monkeypatch, calls)

        response = _post(client, _review_block_actions(review_id))

        assert response.status_code == 200
        body = response.json()
        assert body["decisions"] == [
            {"review_id": review_id, "decision": "approved", "resumed": "scheduled"}
        ]
        assert calls == [incident_id]
        row = _get_review(sqlite_engine, review_id)
        assert row.status == models.ApprovalStatus.APPROVED
        assert row.decided_by == "slack-bob"

    def test_review_reject_records_decision(self, client, sqlite_engine, monkeypatch):
        incident_id = _seed_incident(client)
        review_id = _seed_review(sqlite_engine, incident_id)
        _stub_run(monkeypatch, [])

        response = _post(client, _review_block_actions(review_id, kind="reject"))

        assert response.status_code == 200
        assert response.json()["decisions"][0]["decision"] == "rejected"
        assert _get_review(sqlite_engine, review_id).status == models.ApprovalStatus.REJECTED

    def test_review_duplicate_press_is_200_with_note(self, client, sqlite_engine, monkeypatch):
        incident_id = _seed_incident(client)
        review_id = _seed_review(sqlite_engine, incident_id)
        _stub_run(monkeypatch, [])

        first = _post(client, _review_block_actions(review_id))
        second = _post(client, _review_block_actions(review_id, kind="reject"))

        assert first.status_code == 200 and second.status_code == 200
        assert "note" in second.json()["decisions"][0]

    def test_review_stale_button_is_200_with_note(self, client):
        response = _post(client, _review_block_actions(99999))
        assert response.status_code == 200
        assert response.json()["decisions"][0]["note"].startswith("no such review")


class TestClickFeedback:
    """Live, 2026-10-04: an Approve click on a review card was recorded, but
    the message kept its buttons and the only sign was a line in another
    thread, so it looked like nothing happened. Slack sends a
    `response_url` with every click; the decided message is now replaced
    in place, buttons removed and the verdict shown."""

    def _capture(self, monkeypatch):
        posts = []

        def fake_post(url, json=None, timeout=None):
            posts.append((url, json))
            return httpx.Response(200, request=httpx.Request("POST", url))

        monkeypatch.setattr("helix.api.httpx.post", fake_post)
        return posts

    def _clicked(self, payload: dict) -> dict:
        return {
            **payload,
            "response_url": "https://hooks.slack.test/actions/1",
            "message": {
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": "please review PR #20"}},
                    {"type": "actions", "elements": []},
                ]
            },
        }

    def test_an_approved_review_replaces_its_buttons_with_the_verdict(
        self, client, sqlite_engine, monkeypatch
    ):
        posts = self._capture(monkeypatch)
        incident_id = _seed_incident(client)
        review_id = _seed_review(sqlite_engine, incident_id)
        _stub_run(monkeypatch, [])

        _post(client, self._clicked(_review_block_actions(review_id)))

        [(url, body)] = posts
        assert url == "https://hooks.slack.test/actions/1"
        assert body["replace_original"] is True
        assert all(block["type"] != "actions" for block in body["blocks"])
        assert "please review PR #20" in str(body["blocks"])
        assert "Approved by <@U456>" in str(body["blocks"])

    def test_requested_changes_and_rejections_read_as_such(
        self, client, sqlite_engine, monkeypatch
    ):
        posts = self._capture(monkeypatch)
        incident_id = _seed_incident(client)
        review_id = _seed_review(sqlite_engine, incident_id)
        _stub_run(monkeypatch, [])
        _post(client, self._clicked(_review_block_actions(review_id, kind="reject")))
        assert "Changes requested by <@U456>" in str(posts[-1][1]["blocks"])

    def test_a_click_without_a_response_url_still_decides(self, client, sqlite_engine, monkeypatch):
        posts = self._capture(monkeypatch)
        incident_id = _seed_incident(client)
        review_id = _seed_review(sqlite_engine, incident_id)
        _stub_run(monkeypatch, [])
        response = _post(client, _review_block_actions(review_id))
        assert response.json()["decisions"][0]["decision"] == "approved"
        assert posts == []


def test_a_rejected_approval_reads_as_rejected(client, sqlite_engine, monkeypatch):
    """ "Changes requested" is a code-review word; an approval gate says no."""
    posts = []
    monkeypatch.setattr(
        "helix.api.httpx.post",
        lambda url, json=None, timeout=None: (
            posts.append(json) or httpx.Response(200, request=httpx.Request("POST", url))
        ),
    )
    incident_id = _seed_incident(client)
    approval_id = _seed_pending(sqlite_engine, incident_id)
    _stub_run(monkeypatch, [])
    _post(
        client,
        {
            **_block_actions(approval_id, kind="reject"),
            "response_url": "https://hooks.slack.test/2",
        },
    )
    assert "Rejected by <@U123>" in str(posts[-1]["blocks"])
