"""Subphase 1.9 provenance: the assembled record (state/provenance.py),
the API endpoint, and the CLI's Markdown render - all over seeded rows on
the unit-tier SQLite engine.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from helix.api import app, get_db_session
from helix.cli import main as helix_main
from state import models
from state.db import session_scope
from state.intake import intake_signal
from state.machine import IncidentStatus, transition
from state.provenance import assemble_provenance, render_markdown

K8S_SIGNAL = {
    "source": "kubernetes",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "high",
    "signature": {"waiting_reason": "CrashLoopBackOff", "container": "payment-api"},
    "raw": {"pod": "payment-api-7c886558c8-f29pk", "namespace": "payment-api"},
}

PARAMS = {"path": "sandbox/payment-api/k8s/04-deployment.yaml"}
FINGERPRINT = "f" * 64


def _seed_incident(session) -> models.Incident:
    """One incident straight from intake."""
    return session.scalar(
        select(models.Incident).where(
            models.Incident.incident_id == intake_signal(session, K8S_SIGNAL).incident_id
        )
    )


def _seed_full_chain(session, incident: models.Incident) -> None:
    """Every provenance stage row, as the graph would have written them."""
    now = datetime.now(UTC)
    session.add(
        models.EvidenceItem(
            incident_id=incident.id,
            evidence_id="ev_pods1",
            source="kubernetes",
            kind="pod_list",
            service="payment-api",
            summary="1 pod, 1 not ready",
            payload={"items": []},
            origin="fixture",
            truncated=False,
            total=1,
            collected_at=now,
        )
    )
    session.add(
        models.Hypothesis(
            incident_id=incident.id,
            claim="deployment manifest drifted",
            evidence_refs=["ev_pods1"],
            confidence=0.8,
            contradicting_signals=[],
            attempt=0,
        )
    )
    cand = models.Candidate(
        incident_id=incident.id,
        attempt=0,
        rank=1,
        action="reapply_manifest",
        params=PARAMS,
        rationale="re-assert the committed manifest",
        evidence_refs=["ev_pods1"],
        score=2.0,
        risk_tier="medium",
        selected=True,
        components={"evidence_support": 0.5},
    )
    session.add(cand)
    session.flush()
    session.add(
        models.Validation(
            incident_id=incident.id,
            candidate_id=cand.id,
            attempt=0,
            action="reapply_manifest",
            params=PARAMS,
            fingerprint=FINGERPRINT,
            outcome=models.ValidationOutcome.PASS,
            reason="manifest parses; dry-run clean",
            details={"checks": [{"name": "dry_run", "status": "pass"}]},
        )
    )
    approval = models.Approval(
        incident_id=incident.id,
        candidate_id=cand.id,
        action="reapply_manifest",
        params=PARAMS,
        fingerprint=FINGERPRINT,
        status=models.ApprovalStatus.APPROVED,
        decided_by="carol",
        decided_at=now,
    )
    session.add(approval)
    session.flush()
    session.add(
        models.Execution(
            incident_id=incident.id,
            candidate_id=cand.id,
            approval_id=approval.id,
            attempt=0,
            action="reapply_manifest",
            params=PARAMS,
            fingerprint=FINGERPRINT,
            policy_decision="require_approval",
            verification_required=True,
            decided_by="carol",
            origin="fixture",
            result=models.ExecutionResult.SUCCESS,
            detail="applied",
            output={"op": "apply_manifest"},
            started_at=now,
            finished_at=now,
        )
    )
    session.flush()
    execution = session.scalar(
        select(models.Execution).where(models.Execution.incident_id == incident.id)
    )
    session.add(
        models.Verification(
            incident_id=incident.id,
            execution_id=execution.id,
            candidate_id=cand.id,
            attempt=0,
            action="reapply_manifest",
            params=PARAMS,
            fingerprint=FINGERPRINT,
            outcome=models.VerificationOutcome.PASS,
            detail="all legs held for the window",
            checks=[{"name": "pods_ready", "status": "pass", "detail": "held", "samples": []}],
            window_seconds=60.0,
            poll_seconds=20.0,
            needs_compensation=False,
            started_at=now,
            finished_at=now,
        )
    )
    for status in (
        IncidentStatus.DIAGNOSING,
        IncidentStatus.REMEDIATION_PENDING,
        IncidentStatus.VALIDATING,
        IncidentStatus.EXECUTING,
        IncidentStatus.VERIFYING,
        IncidentStatus.RECOVERED,
    ):
        transition(incident, status, cause="seed", session=session)


class TestAssembleProvenance:
    def test_unknown_incident_returns_none(self, db_session):
        assert assemble_provenance(db_session, "INC-nope") is None

    def test_signal_only_incident_has_trigger_and_transitions(self, db_session):
        incident = _seed_incident(db_session)
        record = assemble_provenance(db_session, incident.incident_id)

        assert record["incident"]["incident_id"] == incident.incident_id
        assert record["incident"]["status"] == "received"
        assert len(record["stages"]["trigger"]) == 1
        assert record["stages"]["trigger"][0]["signature"]["waiting_reason"] == ("CrashLoopBackOff")
        assert [t["to_status"] for t in record["transitions"]] == ["received"]
        # Nothing else has run yet - empty stages, no derived decision.
        assert record["stages"]["evidence"] == []
        assert record["stages"]["decision"] is None
        assert record["stages"]["verification"] == []

    def test_full_chain_covers_every_stage_in_order(self, db_session):
        incident = _seed_incident(db_session)
        _seed_full_chain(db_session, incident)
        record = assemble_provenance(db_session, incident.incident_id)

        stages = record["stages"]
        assert list(stages) == [
            "trigger",
            "evidence",
            "hypothesis",
            "candidates",
            "decision",
            "validation",
            "approval",
            "execution",
            "verification",
            # Phase 2: the change-lifecycle's stages sit after the
            # recovery chain - empty for a recovery incident, populated
            # for a change one.
            "triage",
            "reviews",
            "work_items",
            "knowledge_page",
            # OPT-1: the model's display-only estimate (None without a model).
            "advisory",
        ]
        # A recovery incident's Phase 2 stages are empty but present.
        assert stages["triage"] == []
        assert stages["reviews"] == []
        assert stages["work_items"] == []
        assert stages["knowledge_page"] is None
        assert stages["evidence"][0]["evidence_id"] == "ev_pods1"
        assert stages["evidence"][0]["origin"] == "fixture"
        assert stages["hypothesis"][0]["evidence_refs"] == ["ev_pods1"]
        assert stages["candidates"][0]["selected"] is True
        # The derived decision stage resolves stored ids, not memory.
        decision = stages["decision"]
        assert decision["candidate_id"] == stages["candidates"][0]["id"]
        assert decision["policy_decision"] == "require_approval"
        assert decision["approval_id"] == stages["approval"][0]["id"]
        assert decision["approval_status"] == "approved"
        assert decision["decided_by"] == "carol"
        assert stages["validation"][0]["outcome"] == "pass"
        assert stages["execution"][0]["result"] == "success"
        assert stages["execution"][0]["decided_by"] == "carol"
        verification = stages["verification"][0]
        assert verification["outcome"] == "pass"
        assert verification["execution_id"] == stages["execution"][0]["id"]
        assert verification["needs_compensation"] is False
        assert verification["checks"][0]["name"] == "pods_ready"
        # The full status path is in the transitions timeline.
        path = [t["to_status"] for t in record["transitions"]]
        assert path == [
            "received",
            "diagnosing",
            "remediation_pending",
            "validating",
            "executing",
            "verifying",
            "recovered",
        ]

    def test_record_is_json_serializable(self, db_session):
        import json

        incident = _seed_incident(db_session)
        _seed_full_chain(db_session, incident)
        json.dumps(assemble_provenance(db_session, incident.incident_id))


class TestRenderMarkdown:
    def test_markdown_sections_in_order(self, db_session):
        incident = _seed_incident(db_session)
        _seed_full_chain(db_session, incident)
        text = render_markdown(assemble_provenance(db_session, incident.incident_id))

        headers = [line for line in text.splitlines() if line.startswith("## ")]
        assert headers == [
            "## Trigger",
            "## Evidence",
            "## Hypothesis",
            "## Candidates",
            "## Decision",
            "## Validation",
            "## Approval",
            "## Execution",
            "## Verification",
            "## Transitions",
        ]
        assert f"# Provenance: {incident.incident_id}" in text
        assert "reapply_manifest" in text
        assert "carol" in text
        assert "pass" in text

    def test_markdown_on_bare_incident(self, db_session):
        incident = _seed_incident(db_session)
        text = render_markdown(assemble_provenance(db_session, incident.incident_id))
        assert "none recorded" in text
        assert "no selection recorded" in text


class TestProvenanceApi:
    """GET /incidents/{id}/provenance - the JSON read surface."""

    @pytest.fixture
    def client(self, sqlite_engine):
        def override():
            with session_scope(sqlite_engine) as session:
                yield session

        app.dependency_overrides[get_db_session] = override
        with TestClient(app) as test_client:
            yield test_client
        app.dependency_overrides.clear()

    def test_unknown_incident_404(self, client):
        assert client.get("/incidents/INC-nope/provenance").status_code == 404

    def test_provenance_returns_assembled_record(self, client, sqlite_engine):
        incident_id = client.post("/intake/kubernetes", json=K8S_SIGNAL).json()["incident_id"]
        response = client.get(f"/incidents/{incident_id}/provenance")

        assert response.status_code == 200
        body = response.json()
        assert body["incident"]["incident_id"] == incident_id
        assert list(body["stages"]) == [
            "trigger",
            "evidence",
            "hypothesis",
            "candidates",
            "decision",
            "validation",
            "approval",
            "execution",
            "verification",
            "triage",
            "reviews",
            "work_items",
            "knowledge_page",
            "advisory",
        ]
        assert len(body["stages"]["trigger"]) == 1
        assert [t["to_status"] for t in body["transitions"]] == ["received"]


class TestProvenanceCli:
    """`helix provenance INC-*` prints the Markdown render."""

    def test_cli_prints_markdown(self, sqlite_engine, monkeypatch, capsys):
        monkeypatch.setattr("state.db.session_scope", lambda *a, **kw: session_scope(sqlite_engine))
        with session_scope(sqlite_engine) as session:
            incident = _seed_incident(session)
            incident_id = incident.incident_id
            _seed_full_chain(session, incident)

        rc = helix_main(["provenance", incident_id])

        assert rc == 0
        out = capsys.readouterr().out
        assert out.startswith(f"# Provenance: {incident_id}")
        assert "## Verification" in out
        assert "## Transitions" in out
        assert "reapply_manifest" in out

    def test_cli_unknown_incident_is_clean_error(self, sqlite_engine, monkeypatch, capsys):
        monkeypatch.setattr("state.db.session_scope", lambda *a, **kw: session_scope(sqlite_engine))
        rc = helix_main(["provenance", "INC-nope"])
        assert rc == 2
        assert "no such incident" in capsys.readouterr().err
