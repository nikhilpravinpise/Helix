"""Diagnosis Agent unit tests: deterministic pre-selection, the bounded
evidence loop, evidence persistence, and the bad-evidence-ref re-prompt.

All unit tier: in-memory SQLite, Fixture* evidence capabilities over
recorded fault fixtures, scripted FakeLLM - no live infrastructure, no
real LLM calls.
"""

from fakes import FakeLLM, make_deps
from sqlalchemy import select

from agent.agents.diagnosis import (
    LOW_CONFIDENCE,
    EvidencePlan,
    EvidenceRequest,
    diagnosis_node,
)
from agent.state import Hypothesis
from integrations.evidence import EvidenceKind, EvidenceSource
from state import models
from state.intake import intake_signal

K8S_SIGNAL = {
    "source": "kubernetes",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "high",
    "signature": {"waiting_reason": "CrashLoopBackOff", "container": "payment-api"},
    "raw": {"pod": "payment-api-98867c74d-h6j8v", "namespace": "payment-api"},
}

JENKINS_SIGNAL = {
    "source": "jenkins",
    "service": "payment-api",
    "signature": {"job": "payment-api", "stage": "Test"},
    "raw": {"job": "payment-api", "build_number": 42, "commit": "abc123def456"},
}


def _make_incident(sessions, signal: dict) -> str:
    with sessions() as session:
        return intake_signal(session, signal).incident_id


def _run_diagnosis(deps, sessions, incident_id: str, extra_state: dict | None = None) -> dict:
    """Invoke the diagnosis node directly on a state shaped like the one
    the intake node would have produced."""
    with sessions() as session:
        incident = session.scalar(
            select(models.Incident).where(models.Incident.incident_id == incident_id)
        )
        latest = incident.signals[-1]
        state = {
            "incident_id": incident_id,
            "incident_pk": incident.id,
            "service": incident.service,
            "environment": incident.environment,
            "severity": incident.severity,
            "status": "diagnosing",
            "signal": {
                "source": latest.source,
                "signature": latest.signature,
                "raw": latest.raw,
            },
            "evidence": [],
            "attempts": 0,
            "errors": [],
            **(extra_state or {}),
        }
    return diagnosis_node(deps)(state)


class TestPreselection:
    """Deterministic first pass: the signal source picks the capability
    set before the LLM sees anything."""

    def test_kubernetes_signal_collects_pods_events_secret_logs(self, sqlite_sessions, store):
        deps, caps = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = _run_diagnosis(deps, sqlite_sessions, incident_id)

        k8s_methods = caps["kubernetes"].methods()
        assert "get_pods" in k8s_methods
        assert "get_events" in k8s_methods
        # The pods reference payment-api-secret via envFrom secretRef, so
        # secret_info is requested - the crashloop fixture carries no
        # secret_* evidence, so the miss is recorded, not fatal.
        assert "get_secret_info" in k8s_methods
        secret_calls = [c for c in caps["kubernetes"].calls if c[0] == "get_secret_info"]
        assert any(c[1][1] == "payment-api-secret" for c in secret_calls)
        assert "get_logs" in caps["elk"].methods()
        kinds = {e["kind"] for e in result["evidence"]}
        assert {EvidenceKind.POD_LIST, EvidenceKind.EVENT_LIST, EvidenceKind.LOG_LINES} <= kinds
        assert any("secret_info" in e for e in result["errors"])

    def test_jenkins_signal_collects_console_tests_diff(self, sqlite_sessions, store):
        deps, caps = make_deps(sqlite_sessions, FakeLLM(), "ci/test-regression", store)
        incident_id = _make_incident(sqlite_sessions, JENKINS_SIGNAL)

        result = _run_diagnosis(deps, sqlite_sessions, incident_id)

        jenkins_methods = caps["jenkins"].methods()
        assert "get_console_log" in jenkins_methods
        assert "get_test_results" in jenkins_methods
        # The signal carries a commit sha, so the diff is requested. The
        # ci fixture has no test_report or github.diff artifact, so those
        # misses are recorded - not fabricated, not fatal.
        assert "get_diff" in caps["github"].methods()
        kinds = {e["kind"] for e in result["evidence"]}
        assert EvidenceKind.BUILD_CONSOLE in kinds
        assert any("test_results" in e for e in result["errors"])
        assert any("diff" in e for e in result["errors"])

    def test_jenkins_signal_without_build_number_records_error(self, sqlite_sessions, store):
        deps, caps = make_deps(sqlite_sessions, FakeLLM(), "ci/test-regression", store)
        signal = dict(JENKINS_SIGNAL)
        signal["raw"] = {"job": "payment-api"}
        incident_id = _make_incident(sqlite_sessions, signal)

        result = _run_diagnosis(deps, sqlite_sessions, incident_id)

        assert caps["jenkins"].calls == []
        assert any("job/build_number" in e for e in result["errors"])

    def test_unknown_source_records_error(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        signal = dict(K8S_SIGNAL)
        signal["source"] = "github"
        signal["signature"] = {}
        signal["raw"] = {}
        incident_id = _make_incident(sqlite_sessions, signal)

        result = _run_diagnosis(deps, sqlite_sessions, incident_id)

        assert any("no commit sha" in e for e in result["errors"])
        assert result["evidence"] == []


class TestEvidencePersistence:
    """Every collected Evidence lands in evidence_items - the chain is a
    DB query, not a memory artifact."""

    def test_evidence_items_rows_match_collected(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = _run_diagnosis(deps, sqlite_sessions, incident_id)

        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            rows = session.scalars(
                select(models.EvidenceItem)
                .where(models.EvidenceItem.incident_id == incident.id)
                .order_by(models.EvidenceItem.id)
            ).all()
        collected_ids = [e["id"] for e in result["evidence"]]
        assert [r.evidence_id for r in rows] == collected_ids
        assert len(rows) >= 3
        for row in rows:
            assert row.origin == "fixture"
            assert row.payload  # the full payload is stored, not just the summary

    def test_hypothesis_row_persisted(self, sqlite_sessions, store):
        hyp = Hypothesis(
            claim="payment-api container exits 1 on blank API key",
            evidence_refs=[],  # filled below after collection
            confidence=0.8,
        )
        deps, _ = make_deps(
            sqlite_sessions, FakeLLM([EvidencePlan(requests=[]), hyp]), "cluster/crashloop", store
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = _run_diagnosis(deps, sqlite_sessions, incident_id)
        collected_ids = {e["id"] for e in result["evidence"]}

        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            rows = session.scalars(
                select(models.Hypothesis).where(models.Hypothesis.incident_id == incident.id)
            ).all()
        assert len(rows) == 1
        assert rows[0].claim == hyp.claim
        assert set(rows[0].evidence_refs) <= collected_ids
        assert rows[0].attempt == 0


class TestBoundedLoop:
    """The request-more-evidence loop can never exceed the round cap."""

    def test_round_cap_enforced(self, sqlite_sessions, store):
        # A model that always wants more events: rounds must stop at
        # max_evidence_rounds and interpretation still happens.
        def always_more(prompt, schema):
            if schema is EvidencePlan:
                return EvidencePlan(
                    requests=[
                        EvidenceRequest(
                            source=EvidenceSource.KUBERNETES,
                            kind=EvidenceKind.EVENT_LIST,
                            target="payment-api",
                            rationale="more events",
                        )
                    ]
                )
            return Hypothesis(claim="still crashing", evidence_refs=[], confidence=0.5)

        llm = FakeLLM(script=always_more)
        deps, caps = make_deps(
            sqlite_sessions, llm, "cluster/crashloop", store, max_evidence_rounds=2
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = _run_diagnosis(deps, sqlite_sessions, incident_id)

        assert result["evidence_rounds"] == 2
        # 2 plan rounds + 1 interpretation call.
        structured_calls = [c for c in llm.calls if c[0] == "structured"]
        assert len(structured_calls) == 3
        event_requests = [c for c in caps["kubernetes"].calls if c[0] == "get_events"]
        # 1 pre-selection + 2 loop rounds = 3 total, no runaway.
        assert len(event_requests) == 3
        assert result["hypothesis"] is not None

    def test_empty_requests_ends_loop_early(self, sqlite_sessions, store):
        llm = FakeLLM([EvidencePlan(requests=[])])
        deps, _ = make_deps(sqlite_sessions, llm, "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = _run_diagnosis(deps, sqlite_sessions, incident_id)

        assert result["evidence_rounds"] == 0

    def test_unsatisfiable_request_recorded_not_fatal(self, sqlite_sessions, store):
        llm = FakeLLM(
            [
                EvidencePlan(
                    requests=[
                        EvidenceRequest(
                            source=EvidenceSource.KUBERNETES,
                            kind=EvidenceKind.POD_STATUS,
                            target="",  # no pod name: unsatisfiable
                            rationale="want pod detail",
                        ),
                        EvidenceRequest(
                            source=EvidenceSource.GITHUB,
                            kind=EvidenceKind.DIFF,
                            target="",  # no sha anywhere
                        ),
                    ]
                )
            ]
        )
        deps, _ = make_deps(sqlite_sessions, llm, "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = _run_diagnosis(deps, sqlite_sessions, incident_id)

        assert result["evidence_rounds"] == 1
        assert any("pod name" in e for e in result["errors"])
        assert any("commit sha" in e for e in result["errors"])
        assert result["hypothesis"] is not None


class TestEvidenceRefValidation:
    """A citation of never-collected evidence is a structured-validation
    failure: one re-prompt, then a low-confidence record."""

    def test_bogus_ref_reprompts_then_records_low_confidence(self, sqlite_sessions, store):
        bad = Hypothesis(claim="vibes", evidence_refs=["ev_deadbeef", "ev_notreal"], confidence=0.9)
        llm = FakeLLM([EvidencePlan(requests=[]), bad, bad])
        deps, _ = make_deps(sqlite_sessions, llm, "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = _run_diagnosis(deps, sqlite_sessions, incident_id)

        # 1 plan + 2 interpretation calls (the re-prompt).
        assert len([c for c in llm.calls if c[0] == "structured"]) == 3
        hyp = result["hypothesis"]
        assert hyp["confidence"] <= LOW_CONFIDENCE
        # Dangling refs are dropped so every stored ref resolves.
        assert hyp["evidence_refs"] == []
        assert any("never-collected" in e for e in result["errors"])
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            row = session.scalar(
                select(models.Hypothesis).where(models.Hypothesis.incident_id == incident.id)
            )
        assert row.confidence <= LOW_CONFIDENCE

    def test_bad_ref_fixed_on_reprompt_uses_retry(self, sqlite_sessions, store):
        bad = Hypothesis(claim="vibes", evidence_refs=["ev_deadbeef"], confidence=0.9)
        good_holder: dict = {}

        def fix(prompt: str, schema) -> Hypothesis:
            # The re-prompt names the rejected refs; answer with a ref that
            # exists - read it out of the prompt's evidence list.
            ids = [
                line.split("|")[0].strip("- ").strip()
                for line in prompt.splitlines()
                if line.strip().startswith("- ev_")
            ]
            good_holder["ids"] = ids
            return Hypothesis(claim="real cause", evidence_refs=ids[:1], confidence=0.7)

        llm = FakeLLM([EvidencePlan(requests=[]), bad, fix])
        deps, _ = make_deps(sqlite_sessions, llm, "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = _run_diagnosis(deps, sqlite_sessions, incident_id)

        hyp = result["hypothesis"]
        assert hyp["claim"] == "real cause"
        assert hyp["confidence"] == 0.7
        collected_ids = {e["id"] for e in result["evidence"]}
        assert set(hyp["evidence_refs"]) <= collected_ids
        assert not any("never-collected" in e for e in result["errors"])

    def test_llm_error_in_interpretation_yields_no_hypothesis(self, sqlite_sessions, store):
        from helix.llm import LLMError

        def boom(prompt: str, schema):
            raise LLMError("provider down")

        llm = FakeLLM([EvidencePlan(requests=[]), boom])
        deps, _ = make_deps(sqlite_sessions, llm, "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = _run_diagnosis(deps, sqlite_sessions, incident_id)

        assert result["hypothesis"] is None
        assert result["diagnosis_complete"] is True
        assert any("interpretation failed" in e for e in result["errors"])
