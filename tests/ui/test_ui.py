"""Route tests for the operator UI (subphase 1.12, D11).

Unit tier: TestClient + in-memory SQLite (conftest), real workflow runs
driven through tests/agent/fakes.py make_deps so the provenance timeline
the pages render is produced by the actual code path, not seeded markup.
"""

import pytest
from fakes import K8S_SIGNAL, FakeLLM, make_deps
from sqlalchemy import select

from agent.agents.diagnosis import EvidencePlan
from agent.agents.planner import CandidateList, CandidateProposal
from agent.runner import run_incident
from agent.state import Hypothesis
from state import models
from state.intake import intake_signal

DEPLOYMENT_PATH = "sandbox/payment-api/k8s/04-deployment.yaml"


def _make_incident(sessions, signal: dict) -> str:
    with sessions() as session:
        return intake_signal(session, signal).incident_id


def _manifest_plan(path: str = DEPLOYMENT_PATH) -> CandidateList:
    return CandidateList(
        candidates=[
            CandidateProposal(
                action="reapply_manifest",
                params={"path": path},
                rationale="re-assert the committed manifest",
            )
        ]
    )


def _llm(plans: list[CandidateList], claim: str = "payment-api is wedged") -> FakeLLM:
    return FakeLLM(
        [
            EvidencePlan(requests=[]),
            Hypothesis(claim=claim, evidence_refs=[], confidence=0.8),
            *plans,
        ]
    )


def _run_to_escalated(sqlite_sessions, store) -> tuple[str, object]:
    """Drive a sandbox incident through the full arm-E loop: the
    reapply_manifest write executes, verification fails against the
    still-broken fixture, the compensable action is undone, and the empty
    replan escalates. Every provenance stage except approval lands."""
    deps, _caps = make_deps(sqlite_sessions, _llm([_manifest_plan()]), "cluster/crashloop", store)
    incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
    result = run_incident(incident_id, deps=deps)
    assert result.status == "escalated"
    return incident_id, deps


def _suspend_on_gate(sqlite_sessions, store) -> tuple[str, object]:
    """A production-tier signal + gated manifest action suspends the run
    at awaiting_approval with one pending approvals row."""
    deps, _caps = make_deps(sqlite_sessions, _llm([_manifest_plan()]), "cluster/crashloop", store)
    incident_id = _make_incident(sqlite_sessions, {**K8S_SIGNAL, "environment": "production"})
    result = run_incident(incident_id, deps=deps)
    assert result.status == "awaiting_approval"
    return incident_id, deps


class TestIncidentListPage:
    def test_empty_state(self, client):
        response = client.get("/ui/incidents")
        assert response.status_code == 200
        assert "No incidents yet" in response.text
        assert "helix intake" in response.text

    def test_renders_incident_rows(self, client, sqlite_sessions):
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        response = client.get("/ui/incidents")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        body = response.text
        assert incident_id in body
        assert "payment-api" in body
        assert "sandbox" in body
        assert 'class="badge st-received"' in body
        assert f'href="/ui/incidents/{incident_id}"' in body

    def test_root_redirects_to_ui(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/ui/"
        # ... and following the redirect lands on the list.
        assert client.get("/").status_code == 200

    def test_static_assets_served(self, client):
        css = client.get("/static/style.css")
        assert css.status_code == 200
        assert "text/css" in css.headers["content-type"]
        js = client.get("/static/app.js")
        assert js.status_code == 200
        assert "javascript" in js.headers["content-type"]


class TestIncidentDetailPage:
    def test_unknown_incident_renders_404_page(self, client):
        response = client.get("/ui/incidents/INC-deadbeef")
        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]
        assert "no such incident: INC-deadbeef" in response.text

    def test_full_provenance_stages_render(self, client, sqlite_sessions, store):
        incident_id, _deps = _run_to_escalated(sqlite_sessions, store)
        response = client.get(f"/ui/incidents/{incident_id}")
        assert response.status_code == 200
        body = response.text

        # Header block.
        assert incident_id in body
        assert 'class="badge badge-lg st-escalated"' in body
        assert "Escalated:" in body  # escalation cause callout

        # Every provenance stage renders as a section.
        for stage in (
            "Trigger",
            "Evidence",
            "Hypothesis",
            "Candidates",
            "Decision",
            "Validation",
            "Approval",
            "Execution",
            "Verification",
            "Transitions",
        ):
            assert f">{stage}<" in body, f"missing stage section: {stage}"

        # Entries resolve to stored record ids and timestamps.
        assert "signal #1" in body
        assert "ev_" in body  # evidence content-hash ids
        assert "payment-api is wedged" in body  # hypothesis claim
        assert "reapply_manifest" in body  # candidate + decision + execution
        assert "allow_with_checks" in body  # policy verdict
        assert "compensates" in body  # 1.10 compensation row
        assert "needs compensation" in body
        assert "none required" in body  # approval stage, honestly empty

    def test_nonterminal_page_carries_poll_wiring(self, client, sqlite_sessions, store):
        """The detail page exposes the incident id + status attributes
        app.js polls on - only while the incident is non-terminal."""
        incident_id, _deps = _suspend_on_gate(sqlite_sessions, store)
        response = client.get(f"/ui/incidents/{incident_id}")
        assert response.status_code == 200
        assert f'data-incident-id="{incident_id}"' in response.text
        assert 'data-incident-status="awaiting_approval"' in response.text
        assert 'id="live-check"' in response.text
        # The pending gate is visible on the timeline.
        assert "ap-pending" in response.text

    def test_terminal_page_does_not_poll(self, client, sqlite_sessions, store):
        incident_id, _deps = _run_to_escalated(sqlite_sessions, store)
        response = client.get(f"/ui/incidents/{incident_id}")
        assert 'data-incident-status="escalated"' in response.text
        assert 'id="live-check"' not in response.text


class TestApprovalsPage:
    def test_empty_state(self, client):
        response = client.get("/ui/approvals")
        assert response.status_code == 200
        assert "Nothing awaiting approval" in response.text

    def test_pending_row_renders(self, client, sqlite_sessions, store):
        incident_id, _deps = _suspend_on_gate(sqlite_sessions, store)
        response = client.get("/ui/approvals")
        assert response.status_code == 200
        body = response.text
        assert "reapply_manifest" in body
        assert DEPLOYMENT_PATH in body
        assert incident_id in body
        assert "waiting" in body
        # The decision controls the JS wires onto.
        assert 'data-decide="approve"' in body
        assert 'data-decide="reject"' in body
        assert 'id="actor"' in body

    def test_approve_post_decides_and_page_reflects(
        self, client, sqlite_sessions, store, monkeypatch
    ):
        """The approval button's POST target decides the row and resumes
        the suspended run - monkeypatched to the same fixture deps so the
        resume is real: execution fires, verification fails on the
        still-broken fixture, the incident escalates."""
        incident_id, deps = _suspend_on_gate(sqlite_sessions, store)
        monkeypatch.setattr(
            "agent.runner.run_incident", lambda iid, **kw: run_incident(iid, deps=deps)
        )
        with sqlite_sessions() as session:
            approval_id = session.scalar(
                select(models.Approval)
                .join(models.Incident)
                .where(models.Incident.incident_id == incident_id)
            ).id

        # decided by an owner: only they may decide (state/deciders.py)
        response = client.post(f"/approvals/{approval_id}/approve", json={"actor": "alice"})
        assert response.status_code == 200
        assert response.json()["approval"]["status"] == "approved"
        assert response.json()["resumed"] is True

        page = client.get("/ui/approvals")
        assert "Nothing awaiting approval" in page.text
        assert "ap-approved" in page.text
        assert "alice" in page.text

        # The incident detail now shows the decided approval stage and the
        # post-resume execution + verification stages.
        detail = client.get(f"/ui/incidents/{incident_id}")
        assert "ap-approved" in detail.text
        assert "alice" in detail.text
        assert 'data-incident-status="escalated"' in detail.text

    def test_reject_post_records_decision(self, client, sqlite_sessions, store, monkeypatch):
        incident_id, deps = _suspend_on_gate(sqlite_sessions, store)
        monkeypatch.setattr(
            "agent.runner.run_incident", lambda iid, **kw: run_incident(iid, deps=deps)
        )
        with sqlite_sessions() as session:
            approval_id = session.scalar(
                select(models.Approval)
                .join(models.Incident)
                .where(models.Incident.incident_id == incident_id)
            ).id

        # decided by an owner: only they may decide (state/deciders.py)
        response = client.post(
            f"/approvals/{approval_id}/reject",
            json={"actor": "alice", "note": "too risky today"},
        )
        assert response.status_code == 200
        assert response.json()["approval"]["status"] == "rejected"

        page = client.get("/ui/approvals")
        assert "ap-rejected" in page.text
        assert "too risky today" in page.text


@pytest.mark.integration
def test_ui_against_real_postgres(pg):
    """The unmodified app (no dependency override) renders real pages over
    compose Postgres - including the live-status poll target."""
    from fastapi.testclient import TestClient

    from helix.api import app

    signal = {
        "source": "kubernetes",
        "service": "payment-api",
        "environment": "sandbox",
        "signature": {"waiting_reason": "CrashLoopBackOff", "container": "payment-api"},
        "raw": {"pod": "payment-api-ui-check"},
    }
    with TestClient(app) as test_client:
        incident_id = test_client.post("/intake/kubernetes", json=signal).json()["incident_id"]

        listing = test_client.get("/ui/incidents")
        assert listing.status_code == 200
        assert incident_id in listing.text

        detail = test_client.get(f"/ui/incidents/{incident_id}")
        assert detail.status_code == 200
        assert f'data-incident-id="{incident_id}"' in detail.text
        # The poll target: the JSON read endpoint returns current state.
        poll = test_client.get(f"/incidents/{incident_id}")
        assert poll.status_code == 200
        assert poll.json()["status"] == "received"

        assert test_client.get("/ui/approvals").status_code == 200
