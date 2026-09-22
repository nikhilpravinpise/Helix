"""Subphase 2.4 page-builder tests: `build_incident_page` renders the
full provenance chain (state/provenance.py) plus the incident row's
timeline/triage/review/work-item state as real Notion API blocks, over a
seeded SQLite incident - no Notion, no network.
"""

import json
from datetime import UTC, datetime

from sqlalchemy import select

from integrations.notion.pages import build_incident_page
from state import models
from state.intake import intake_signal
from state.machine import IncidentStatus, transition

GH_SIGNAL = {
    "source": "github",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "high",
    "signature": {"pr": 184, "action": "opened"},
    "raw": {"pull_request": {"number": 184}},
}

PARAMS = {"path": "sandbox/payment-api/k8s/04-deployment.yaml"}
FINGERPRINT = "f" * 64


def _incident(session) -> models.Incident:
    """One change-kind incident straight from intake."""
    incident = session.scalar(
        select(models.Incident).where(
            models.Incident.incident_id == intake_signal(session, GH_SIGNAL).incident_id
        )
    )
    incident.kind = models.IncidentKind.CHANGE
    return incident


def _seed_full_chain(session, incident: models.Incident) -> None:
    """Every row a completed unhappy-path incident carries: the section 14
    lifecycle (review, deploy, failed deploy -> recovery) plus triage,
    review and work-item rows - as the graph would have written them."""
    now = datetime.now(UTC)
    session.add(
        models.TriageResult(
            incident_id=incident.id,
            affected_service="payment-api",
            blast_radius="medium",
            owner="team-payments",
            owner_source="registry",
            rationale="PR touches the payment-api manifests",
        )
    )
    session.add(
        models.ReviewRequest(
            incident_id=incident.id,
            reviewer="alice",
            rationale="CODEOWNERS for sandbox/payment-api",
            status=models.ApprovalStatus.APPROVED,
            decided_by="alice",
            decided_at=now,
            note="lgtm",
        )
    )
    session.add(
        models.WorkItem(
            incident_id=incident.id,
            provider="jira",
            external_id="10001",
            external_key="HELIX-1",
            external_url="https://jira.example/browse/HELIX-1",
            title="track it",
            status="done",
        )
    )
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
            claim="deployed manifest drifted from repo",
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
        note="safe: sandbox",
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
            checks=[{"name": "pods_ready", "status": "pass", "detail": "held"}],
            window_seconds=60.0,
            poll_seconds=20.0,
            needs_compensation=False,
            started_at=now,
            finished_at=now,
        )
    )
    # The section 14 unhappy path in full: change lifecycle -> failed
    # deploy -> recovery loop -> recovered.
    for status in (
        IncidentStatus.TRIAGED,
        IncidentStatus.OWNER_ASSIGNED,
        IncidentStatus.REVIEW_PENDING,
        IncidentStatus.APPROVED,
        IncidentStatus.DEPLOYING,
        IncidentStatus.FAILED,
        IncidentStatus.DIAGNOSING,
        IncidentStatus.REMEDIATION_PENDING,
        IncidentStatus.VALIDATING,
        IncidentStatus.EXECUTING,
        IncidentStatus.VERIFYING,
        IncidentStatus.RECOVERED,
    ):
        transition(incident, status, cause="seed", session=session)


def _headings(blocks: list[dict]) -> list[str]:
    return [
        b["heading_2"]["rich_text"][0]["text"]["content"]
        for b in blocks
        if b["type"] == "heading_2"
    ]


def _bullets(blocks: list[dict]) -> list[str]:
    return [
        b["bulleted_list_item"]["rich_text"][0]["text"]["content"]
        for b in blocks
        if b["type"] == "bulleted_list_item"
    ]


def _paragraphs(blocks: list[dict]) -> list[str]:
    return [
        b["paragraph"]["rich_text"][0]["text"]["content"]
        for b in blocks
        if b["type"] == "paragraph"
    ]


def _texts(blocks: list[dict]) -> list[str]:
    """Every rich_text content string, whatever the block type."""
    out = []
    for b in blocks:
        for rt in b.get(b["type"], {}).get("rich_text", []):
            out.append(rt["text"]["content"])
    return out


class TestBuildIncidentPage:
    def test_title_format(self, sqlite_sessions):
        with sqlite_sessions() as session:
            incident = _incident(session)
            title, _ = build_incident_page(session, incident)
        assert title == f"{incident.incident_id}: payment-api (received)"

    def test_full_page_renders_every_section(self, sqlite_sessions):
        with sqlite_sessions() as session:
            incident = _incident(session)
            _seed_full_chain(session, incident)
            title, blocks = build_incident_page(session, incident)

        assert title == f"{incident.incident_id}: payment-api (recovered)"
        assert _headings(blocks) == [
            "Summary",
            "Timeline",
            "Trigger",
            "Triage",
            "Review requests",
            "Evidence",
            "Hypotheses",
            "Candidates",
            "Decision",
            "Actions taken",
            "Validations",
            "Approvals",
            "Verifications",
            "Work items",
            "Outcome and follow-ups",
        ]

        paragraphs = _paragraphs(blocks)
        assert paragraphs[0].startswith("Synced at ")
        assert "payment-api" in paragraphs[1]
        assert "severity high" in paragraphs[1]
        assert "status recovered" in paragraphs[1]

        bullets = _bullets(blocks)
        joined = "\n".join(bullets)
        # The full provenance chain renders stored data, not placeholders.
        assert "signal #1 (github)" in joined
        assert '"pr": 184' in joined
        assert "affected service payment-api; blast radius medium" in joined
        assert "owner team-payments via registry" in joined
        assert "alice: approved - CODEOWNERS" in joined
        assert "ev_pods1 (kubernetes/pod_list, fixture)" in joined
        assert "1 pod, 1 not ready" in joined
        assert "deployed manifest drifted" in joined
        assert "reapply_manifest" in joined and "[selected]" in joined
        assert "policy: require_approval" in joined
        assert "approved by carol" in joined
        assert "safe: sandbox" in joined
        assert ": success [policy=require_approval, origin=fixture]" in joined
        assert "manifest parses; dry-run clean" in joined
        assert "pods_ready=pass" in joined
        assert "jira HELIX-1: done - https://jira.example/browse/HELIX-1" in joined
        assert "outcome: recovered" in joined
        assert "no open follow-ups" in joined
        # The whole lifecycle path is on the timeline.
        for status in (
            "triaged",
            "owner_assigned",
            "deploying",
            "failed",
            "diagnosing",
            "recovered",
        ):
            assert f"-> {status}: seed" in joined
        assert "(new) -> received" in joined

    def test_blocks_are_valid_notion_shape_and_json_safe(self, sqlite_sessions):
        with sqlite_sessions() as session:
            incident = _incident(session)
            _seed_full_chain(session, incident)
            _, blocks = build_incident_page(session, incident)

        for b in blocks:
            assert b["object"] == "block"
            assert b["type"] in ("heading_2", "paragraph", "bulleted_list_item", "divider")
            assert b["type"] in b
            if b["type"] == "divider":
                assert b["divider"] == {}
            else:
                rt = b[b["type"]]["rich_text"]
                assert all(r["type"] == "text" for r in rt)
        json.dumps(blocks)  # must not raise - pure JSON over the wire

    def test_bare_incident_renders_stable_template(self, sqlite_sessions):
        with sqlite_sessions() as session:
            incident = _incident(session)
            title, blocks = build_incident_page(session, incident)

        assert title == f"{incident.incident_id}: payment-api (received)"
        bullets = _bullets(blocks)
        # Every section exists; the ones nothing has written yet say so
        # (Triage, Review requests, Evidence, Hypotheses, Candidates,
        # Decision, Actions taken, Validations, Approvals, Verifications,
        # Work items).
        assert bullets.count("none recorded") == 11
        assert any("(new) -> received" in b for b in bullets)
        assert "signal #1 (github)" in "\n".join(bullets)
        assert "outcome: received" in "\n".join(bullets)
        assert "no open follow-ups" in "\n".join(bullets)

    def test_long_text_is_clipped_to_notion_limit(self, sqlite_sessions):
        with sqlite_sessions() as session:
            incident = _incident(session)
            session.add(
                models.EvidenceItem(
                    incident_id=incident.id,
                    evidence_id="ev_big",
                    source="elk",
                    kind="log_lines",
                    service="payment-api",
                    summary="E" * 5000,
                    payload={},
                    origin="fixture",
                    truncated=True,
                    total=5000,
                    collected_at=datetime.now(UTC),
                )
            )
            _, blocks = build_incident_page(session, incident)

        texts = _texts(blocks)
        assert texts
        assert all(len(t) <= 1900 for t in texts)
        clipped = [t for t in texts if t.endswith("...")]
        assert clipped and clipped[0].startswith("ev_big (elk/log_lines, fixture)")

    def test_render_is_deterministic_apart_from_sync_marker(self, sqlite_sessions):
        with sqlite_sessions() as session:
            incident = _incident(session)
            _seed_full_chain(session, incident)
            _, first = build_incident_page(session, incident)
            _, second = build_incident_page(session, incident)

        # The drain appends each sync wholesale; identical state must
        # render identical blocks except the Synced-at line itself.
        assert first[0]["paragraph"]["rich_text"][0]["text"]["content"].startswith("Synced at ")
        assert json.dumps(first[1:]) == json.dumps(second[1:])
