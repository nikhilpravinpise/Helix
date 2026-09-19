"""Retrieval over incident history (subphase E6).

The exit criterion in two halves: for a scenario resembling a previously
resolved incident, retrieval returns that incident with a citation
resolving to a real record, and ablation arm C runs end to end.

The arm-C composition is checked here; arm C actually reasoning needs an
LLM and is covered by the benchmark, which skips it honestly without a
key.
"""

import pytest

from agent.retrieval_context import prior_incident_context, retrieval_query
from events.retrieval import (
    Citation,
    CitationError,
    build_document,
    index_incidents,
    render_context,
    resolve_citation,
    score_documents,
    similar_incidents,
    tokenize,
)
from state import models
from state.intake import intake_signal
from state.machine import IncidentStatus, transition

CRASHLOOP = {
    "source": "kubernetes",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "high",
    "signature": {
        "waiting_reason": "CrashLoopBackOff",
        "container": "payment-api",
        "terminated_reason": "Error",
    },
}
OOM = {
    **CRASHLOOP,
    "signature": {
        "waiting_reason": "CrashLoopBackOff",
        "container": "payment-api",
        "terminated_reason": "OOMKilled",
    },
}

RUN = [
    (IncidentStatus.DIAGNOSING, "d"),
    (IncidentStatus.REMEDIATION_PENDING, "p"),
    (IncidentStatus.VALIDATING, "v"),
    (IncidentStatus.EXECUTING, "e"),
    (IncidentStatus.VERIFYING, "x"),
    (IncidentStatus.RECOVERED, "ok"),
]


def resolved_incident(
    session, signal=CRASHLOOP, claim="pod crashlooped", action="reapply_manifest"
):
    """An incident driven to recovered, with a hypothesis and a selected
    action - the shape retrieval is meant to find."""
    result = intake_signal(session, signal)
    incident = (
        session.query(models.Incident)
        .filter(models.Incident.incident_id == result.incident_id)
        .one()
    )
    session.add(
        models.Hypothesis(
            incident_id=incident.id,
            claim=claim,
            evidence_refs=[],
            confidence=0.9,
            contradicting_signals=[],
        )
    )
    session.add(
        models.Candidate(
            incident_id=incident.id,
            action=action,
            params={},
            rationale="because",
            evidence_refs=[],
            rank=1,
            score=1.0,
            risk_tier="low",
            components={},
            selected=True,
        )
    )
    session.flush()
    for status, cause in RUN:
        transition(incident, status, cause=cause, session=session)
    session.flush()
    return incident


class TestTokenize:
    def test_domain_terms_stay_whole(self):
        """`CrashLoopBackOff` and `payment-api` are single terms here;
        splitting them would destroy the most discriminating part of a
        fault description."""
        assert "crashloopbackoff" in tokenize("CrashLoopBackOff")
        assert "payment-api" in tokenize("payment-api failed")
        assert "spec.template.spec" in tokenize("spec.template.spec invalid")

    def test_stopwords_and_single_characters_are_dropped(self):
        assert tokenize("the a of x pod") == ["pod"]


class TestDocuments:
    def test_document_covers_summary_hypothesis_action_and_outcome(self, db_session):
        incident = resolved_incident(db_session)
        doc = build_document(incident)
        for expected in ("payment-api", "crashloopbackoff", "crashlooped", "reapply_manifest"):
            assert expected in doc.text.lower()
        assert doc.summary["outcome"] == "recovered"

    def test_only_resolved_incidents_are_indexed(self, db_session):
        """An incident still in flight has no outcome to learn from, and an
        escalated one has no fix to reuse - offering either as precedent
        would be offering a guess as experience."""
        prior = resolved_incident(db_session)
        intake_signal(db_session, OOM)  # left at received
        db_session.flush()
        assert [d.incident_id for d in index_incidents(db_session)] == [prior.incident_id]


class TestScoring:
    def test_rare_terms_outweigh_common_ones(self, db_session):
        """Without IDF weighting every incident for one service would look
        equally similar to every other."""
        a = build_document(resolved_incident(db_session, CRASHLOOP, claim="disk full"))
        b = build_document(
            resolved_incident(db_session, OOM, claim="container exceeded its memory limit")
        )
        scored = score_documents("memory limit exceeded", [a, b])
        assert scored[0][0].incident_id == b.incident_id

    def test_no_shared_terms_scores_nothing(self, db_session):
        doc = build_document(resolved_incident(db_session))
        assert score_documents("entirely unrelated vocabulary", [doc]) == []

    def test_empty_query_returns_nothing(self, db_session):
        doc = build_document(resolved_incident(db_session))
        assert score_documents("", [doc]) == []

    def test_results_are_explainable(self, db_session):
        """matched_terms is what makes a result auditable rather than an
        oracle."""
        resolved_incident(db_session)
        results = similar_incidents(db_session, "CrashLoopBackOff payment-api")
        assert results
        assert "crashloopbackoff" in results[0].matched_terms


class TestSimilarIncidents:
    def test_a_resembling_scenario_retrieves_the_prior_incident(self, db_session):
        """The exit criterion's first half."""
        prior = resolved_incident(db_session)
        results = similar_incidents(
            db_session, "payment-api CrashLoopBackOff container payment-api terminated Error"
        )
        assert results
        assert results[0].citation.incident_id == prior.incident_id

    def test_an_incident_does_not_retrieve_itself(self, db_session):
        prior = resolved_incident(db_session)
        results = similar_incidents(
            db_session, "CrashLoopBackOff payment-api", exclude=prior.incident_id
        )
        assert all(r.citation.incident_id != prior.incident_id for r in results)

    def test_weak_coincidence_is_not_offered_as_precedent(self, db_session):
        """Returning nothing is a better answer than returning noise."""
        resolved_incident(db_session)
        assert similar_incidents(db_session, "quarterly billing export", min_score=0.5) == []

    def test_results_are_bounded(self, db_session):
        for i in range(5):
            resolved_incident(db_session, claim=f"crashlooped variant {i}")
        assert len(similar_incidents(db_session, "crashlooped payment-api", limit=2)) == 2

    def test_no_history_returns_nothing(self, db_session):
        assert similar_incidents(db_session, "anything") == []


class TestCitations:
    def test_citation_resolves_to_real_records(self, db_session):
        """The exit criterion's second half: a citation that cannot be
        resolved launders a guess into a reference."""
        prior = resolved_incident(db_session)
        result = similar_incidents(db_session, "CrashLoopBackOff payment-api")[0]
        resolved = resolve_citation(db_session, result.citation)
        assert resolved["incident"].incident_id == prior.incident_id
        assert resolved["hypotheses"][0].claim == "pod crashlooped"
        assert resolved["candidates"][0].action == "reapply_manifest"

    def test_an_unknown_incident_raises(self, db_session):
        with pytest.raises(CitationError, match="unknown incident"):
            resolve_citation(db_session, Citation(incident_id="INC-nope"))

    def test_a_dangling_hypothesis_reference_raises(self, db_session):
        prior = resolved_incident(db_session)
        with pytest.raises(CitationError, match="hypotheses"):
            resolve_citation(
                db_session, Citation(incident_id=prior.incident_id, hypothesis_ids=(9999,))
            )

    def test_citation_renders_readably(self, db_session):
        prior = resolved_incident(db_session)
        rendered = similar_incidents(db_session, "CrashLoopBackOff")[0].citation.render()
        assert prior.incident_id in rendered


class TestRenderedContext:
    def test_every_line_carries_its_citation(self, db_session):
        resolved_incident(db_session)
        results = similar_incidents(db_session, "CrashLoopBackOff payment-api")
        rendered = render_context(results)
        assert results[0].citation.incident_id in rendered

    def test_context_says_precedent_is_not_evidence(self, db_session):
        """The model must not be able to support a claim with a prior
        incident in place of collected evidence."""
        resolved_incident(db_session)
        rendered = render_context(similar_incidents(db_session, "CrashLoopBackOff"))
        assert "not evidence" in rendered

    def test_no_results_renders_nothing(self):
        assert render_context([]) == ""


class TestFlagGating:
    """Retrieval's effect has to be measured rather than assumed, so it is
    off unless switched on."""

    def test_query_uses_the_fault_not_the_conclusion(self):
        """Using a hypothesis would make retrieval depend on the answer it
        is meant to help produce."""
        query = retrieval_query(
            {"service": "payment-api", "environment": "sandbox", "severity": "high"},
            {"waiting_reason": "CrashLoopBackOff"},
        )
        assert "payment-api" in query
        assert "CrashLoopBackOff" in query

    def test_context_is_empty_when_disabled(self, db_session):
        class Deps:
            def session_scope(self):
                raise AssertionError("retrieval must not touch the database when disabled")

        assert prior_incident_context(Deps(), {"service": "payment-api"}, {}) == ""

    def test_context_is_built_when_enabled(self, db_session, sqlite_engine):
        import contextlib

        resolved_incident(db_session)
        db_session.commit()

        class Deps:
            @contextlib.contextmanager
            def session_scope(self):
                yield db_session

        context = prior_incident_context(
            Deps(),
            {"service": "payment-api", "environment": "sandbox", "severity": "high"},
            {"waiting_reason": "CrashLoopBackOff"},
            enabled=True,
        )
        assert "Prior resolved incidents" in context

    def test_a_retrieval_failure_does_not_fail_the_run(self):
        """An aid that can fail the run is worse than no aid: a run that
        would have succeeded must not start failing because retrieval was
        switched on."""

        class BrokenDeps:
            def session_scope(self):
                raise RuntimeError("database gone")

        assert prior_incident_context(BrokenDeps(), {"service": "x"}, {}, enabled=True) == ""


class TestArmC:
    """Arm C is B plus retrieval - identical composition, so the ablation
    isolates what RAG buys over a bare LLM."""

    def test_arm_c_is_a_known_arm(self):
        from agent.graph import _ARM_NODES

        assert "C" in _ARM_NODES

    def test_arm_c_composes_the_same_nodes_as_b(self):
        from agent.graph import _ARM_NODES

        assert _ARM_NODES["C"] == _ARM_NODES["B"]

    def test_the_benchmark_accepts_arm_c(self):
        from benchmark.tolerance import BANDS

        assert "C" in BANDS

    def test_arm_c_skips_honestly_without_a_key(self, monkeypatch):
        from benchmark import catalog
        from benchmark.harness import HarnessConfig, run_scenario

        monkeypatch.delenv("HELIX_LLM_API_KEY", raising=False)
        scenarios = catalog.load_scenarios()
        record = run_scenario(
            "ci-test-regression",
            scenarios["ci-test-regression"],
            "C",
            HarnessConfig(arms=["C"], database_url="postgresql+psycopg://x@127.0.0.1:59999/x"),
        )
        assert record.skipped == "arm C requires HELIX_LLM_API_KEY"
