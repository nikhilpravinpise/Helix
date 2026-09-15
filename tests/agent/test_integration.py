"""Integration tier for subphase 1.5 (D9): the pieces that need live
infrastructure.

- Checkpoint round-trip on the compose Postgres: run interrupted before
  diagnosis, then resumed through `run_incident` with a NEW checkpointer
  connection - suspend/resume working across processes is the D13 point.
- The workflow_runs partial unique index as a real database guarantee.
- Live Groq round-trips of complete() / complete_structured() - the first
  real provider call ever, where any drift in model id, temperature/seed
  acceptance, or JSON mode surfaces. Then three distinct fixtures through
  diagnosis with the real model; every cited ref must resolve.

Deselected by default (`addopts = -m 'not integration'`); run with
`uv run pytest -m integration`. The live-LLM tests additionally skip when
HELIX_LLM_API_KEY is unset.
"""

import pytest
from fakes import FakeLLM, make_deps
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agent.graph import build_graph, open_checkpointer
from agent.runner import run_incident
from helix.config import get_settings
from helix.llm import OpenAICompatibleLLMClient
from state import models
from state.intake import intake_signal
from state.machine import IncidentStatus

pytestmark = pytest.mark.integration


def _make_incident(sessions, signal: dict) -> str:
    with sessions() as session:
        return intake_signal(session, signal).incident_id


class TestCheckpointRoundTrip:
    """D13 made real: PostgresSaver with thread_id = incident_id."""

    def test_suspend_and_resume_across_connections(self, pg, pg_sessions, store):
        deps, _ = make_deps(
            pg_sessions,
            FakeLLM(),
            "cluster/crashloop",
            store,
            database_url=get_settings().database_url,
        )
        incident_id = _make_incident(pg_sessions, K8S_SIGNAL_PG)
        config = {"configurable": {"thread_id": incident_id}}

        # Phase 1: one "process" runs the graph, parked before diagnosis.
        with open_checkpointer(deps.database_url) as saver:
            graph = build_graph(deps, checkpointer=saver, interrupt_before=["diagnosis"])
            state = graph.invoke({"incident_id": incident_id}, config)
            assert state["status"] == IncidentStatus.DIAGNOSING
            assert graph.get_state(config).next == ("diagnosis",)
        with pg_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert incident.status == IncidentStatus.DIAGNOSING
            # Park the run row so run_incident resumes it in place.
            session.add(
                models.WorkflowRun(incident_id=incident.id, status=models.RunStatus.SUSPENDED)
            )

        # Phase 2: a different process - fresh checkpointer connection -
        # resumes by thread_id alone. run_incident's suspended-run path
        # reuses the run row and invokes with no input.
        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.REMEDIATION_PENDING
        assert result.run_status == models.RunStatus.TERMINAL
        with pg_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert incident.status == IncidentStatus.REMEDIATION_PENDING
            assert len(incident.evidence_items) >= 3
            assert len(incident.hypotheses) == 1
            # The suspended run row was resumed and finished, not replaced.
            runs = [r.status for r in incident.workflow_runs]
            assert runs == [models.RunStatus.TERMINAL]


class TestRunGuardIndex:
    """The partial unique index makes 'one running run per incident' a
    database guarantee, not a check-then-act."""

    def test_second_running_row_rejected_by_database(self, pg, pg_sessions):
        incident_id = _make_incident(pg_sessions, K8S_SIGNAL_PG)
        with pg_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            session.add(
                models.WorkflowRun(incident_id=incident.id, status=models.RunStatus.RUNNING)
            )

        with pytest.raises(IntegrityError):
            with pg_sessions() as session:
                session.add(
                    models.WorkflowRun(incident_id=incident.id, status=models.RunStatus.RUNNING)
                )


K8S_SIGNAL_PG = {
    "source": "kubernetes",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "high",
    "signature": {"waiting_reason": "CrashLoopBackOff", "container": "payment-api"},
    "raw": {"pod": "payment-api-98867c74d-h6j8v", "namespace": "payment-api"},
}


# --- Live LLM tier: the first real provider round-trip (D14) ---

requires_llm_key = pytest.mark.skipif(
    not get_settings().llm_api_key, reason="HELIX_LLM_API_KEY not set"
)


class _Answer(BaseModel):
    answer: str


@requires_llm_key
class TestLiveLLMRoundTrip:
    """complete() and complete_structured() against the real provider:
    pinned model id, temperature=0 + seed acceptance, and JSON-mode
    structured output - never before verified live."""

    def test_complete_roundtrip(self):
        client = OpenAICompatibleLLMClient()
        response = client.complete("Reply with the single word: ok")
        assert response.text.strip()
        assert response.model == get_settings().llm_model

    def test_complete_structured_roundtrip(self):
        client = OpenAICompatibleLLMClient()
        result = client.complete_structured(
            'Reply with a JSON object whose only field is answer, set to the string "ok".',
            _Answer,
        )
        assert result.answer.strip().lower().strip('."') == "ok"

    def test_determinism_identical_calls(self):
        # Pinned model + temperature 0 + fixed seed: two identical calls
        # must return identical text - the determinism policy is only real
        # if the provider honors it.
        client = OpenAICompatibleLLMClient()
        prompt = "Reply with exactly: helix determinism check"
        assert client.complete(prompt).text == client.complete(prompt).text


@requires_llm_key
class TestLiveDiagnosis:
    """Three distinct fault fixtures through diagnosis with the real
    model. Structural guarantees are asserted here (every cited ref
    resolves to collected evidence, claim non-empty); whether the claim
    is *correct* is reviewed in the PR - the exit criterion is that the
    cited evidence exists and supports it."""

    @pytest.mark.parametrize(
        "fixture,waiting_reason",
        [
            ("cluster/crashloop", "CrashLoopBackOff"),
            ("cluster/oom-killed", "CrashLoopBackOff"),
            ("cluster/missing-secret", "CreateContainerConfigError"),
        ],
    )
    def test_fixture_diagnosis(self, pg, pg_sessions, store, fixture, waiting_reason):
        deps, _ = make_deps(
            pg_sessions,
            OpenAICompatibleLLMClient(),
            fixture,
            store,
            database_url=get_settings().database_url,
        )
        signal = dict(K8S_SIGNAL_PG)
        signal["signature"] = {
            "waiting_reason": waiting_reason,
            "container": "payment-api",
        }
        incident_id = _make_incident(pg_sessions, signal)

        result = run_incident(incident_id, deps=deps)

        assert result.error is None
        assert result.status == IncidentStatus.REMEDIATION_PENDING
        assert result.hypothesis is not None
        hyp = result.hypothesis
        assert hyp["claim"].strip()
        assert 0.0 <= hyp["confidence"] <= 1.0

        # Every cited ref must resolve to evidence actually collected -
        # cited-evidence existence is what the exit criterion pins down.
        with pg_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            stored_ids = {row.evidence_id for row in incident.evidence_items}
            assert len(incident.evidence_items) >= 3
        assert hyp["evidence_refs"], "hypothesis cites nothing"
        assert set(hyp["evidence_refs"]) <= stored_ids
