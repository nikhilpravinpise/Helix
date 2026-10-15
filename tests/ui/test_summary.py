"""The incident page's plain-English summary card: what broke, why, what
Helix did, and the outcome - built by helix.ui._summary from the same
provenance record the timeline renders, over real runner-driven incidents."""

from fakes import JENKINS_SIGNAL, K8S_SIGNAL, FakeLLM, make_deps
from sqlalchemy import select
from test_ui import _make_incident, _run_to_escalated, _suspend_on_gate
from test_verification import K8S, _healthy_doc, _llm, _plan

from agent.runner import run_incident
from events.ingest import ingest_event
from events.schemas import EngineeringEvent, EventSource, EventType
from helix import ui
from state import models
from state.provenance import assemble_provenance


def _rows(sessions, incident_id: str) -> dict[str, str]:
    with sessions() as session:
        record = assemble_provenance(session, incident_id)
    summary = ui._summary(record)
    return {r["label"]: r["text"] for r in summary["rows"]}


def _recovered(sqlite_sessions, store) -> str:
    plan = _plan("restart_deployment", dict(K8S), "bounce the wedged pod")
    deps, _ = make_deps(sqlite_sessions, _llm([plan]), _healthy_doc(), store)
    incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
    assert run_incident(incident_id, deps=deps).status == "recovered"
    return incident_id


def _review_pending(sqlite_sessions, store) -> str:
    deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
    with sqlite_sessions() as session:
        event = EngineeringEvent(
            event_type=EventType.PR_OPENED,
            source=EventSource.GITHUB,
            service="payment-api",
            environment="sandbox",
            actor="dana",
            metadata={
                "pr": 42,
                "title": "Fix payment config",
                "branch": "feature/fix-config",
                "sha": "a" * 40,
                "base": "main",
                "files": ["sandbox/payment-api/k8s/02-configmap.yaml"],
                "action": "opened",
            },
        )
        incident_id = ingest_event(session, event).incident_id
        session.commit()
    run_incident(incident_id, deps=deps, arm="A")
    assert run_incident(incident_id, deps=deps, arm="A").status == "review_pending"
    return incident_id


class TestSummary:
    def test_recovered(self, sqlite_sessions, store):
        rows = _rows(sqlite_sessions, _recovered(sqlite_sessions, store))
        assert list(rows) == ["What broke", "Why", "What Helix did", "Outcome"]
        assert "Kubernetes" in rows["What broke"]
        assert "payment-api" in rows["What broke"]
        assert "CrashLoopBackOff" in rows["What broke"]
        assert "payment-api is wedged" in rows["Why"]
        assert "0.80" in rows["Why"]
        assert "restart_deployment" in rows["What Helix did"]
        assert "success" in rows["What Helix did"]
        assert rows["Outcome"].startswith("Recovered")
        assert "verification pass" in rows["Outcome"]

    def test_jenkins_trigger_names_job_and_stage(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, JENKINS_SIGNAL)
        rows = _rows(sqlite_sessions, incident_id)
        assert "jenkins" in rows["What broke"].lower()
        assert "payment-api" in rows["What broke"]
        assert "Test" in rows["What broke"]
        # Nothing but the trigger exists yet: the other rows are omitted.
        assert "Why" not in rows
        assert deps is not None

    def test_escalated(self, sqlite_sessions, store):
        incident_id, _deps = _run_to_escalated(sqlite_sessions, store)
        rows = _rows(sqlite_sessions, incident_id)
        assert "reapply_manifest" in rows["What Helix did"]
        assert "undone" in rows["What Helix did"]
        assert rows["Outcome"].startswith("Escalated")
        assert "verification fail" in rows["Outcome"]
        with sqlite_sessions() as session:
            cause = assemble_provenance(session, incident_id)["incident"]["escalation_cause"]
        assert cause in rows["Outcome"]

    def test_awaiting_approval_says_so(self, sqlite_sessions, store):
        incident_id, _deps = _suspend_on_gate(sqlite_sessions, store)
        rows = _rows(sqlite_sessions, incident_id)
        assert rows["What Helix did"].startswith("Nothing executed")
        assert "waiting for approval" in rows["What Helix did"]
        assert "reapply_manifest" in rows["What Helix did"]
        assert rows["Outcome"].startswith("Awaiting approval")

    def test_change_incident_review_pending(self, sqlite_sessions, store):
        incident_id = _review_pending(sqlite_sessions, store)
        rows = _rows(sqlite_sessions, incident_id)
        assert rows["What broke"] == "Change to payment-api"
        assert "alice" in rows["Why"]  # triage owner
        with sqlite_sessions() as session:
            reviewer = session.scalar(select(models.ReviewRequest)).reviewer
        assert rows["What Helix did"].startswith("Nothing executed")
        assert f"review pending with {reviewer}" in rows["What Helix did"]
        assert rows["Outcome"].startswith("Review pending")

    def test_pr_url_is_linked_only_when_http(self):
        def record(url):
            return {
                "incident": {"status": "recovered", "kind": "failure", "service": "s"},
                "stages": {
                    "trigger": [],
                    "execution": [
                        {
                            "id": 1,
                            "action": "open_fix_pr",
                            "result": "success",
                            "detail": "opened",
                            "output": {"pr_url": url, "branch": "fix/x"},
                            "compensates_id": None,
                            "decided_by": "alice",
                        }
                    ],
                },
            }

        did = next(
            r
            for r in ui._summary(record("https://gh/pr/7"))["rows"]
            if r["label"] == "What Helix did"
        )
        assert did["link"] == "https://gh/pr/7"
        assert "fix/x" in did["text"]
        assert "approved by alice" in did["text"]
        did = next(
            r
            for r in ui._summary(record("javascript:alert(1)"))["rows"]
            if r["label"] == "What Helix did"
        )
        assert did.get("link") is None


class TestSummaryPage:
    def test_card_renders_on_page(self, client, sqlite_sessions, store):
        incident_id = _recovered(sqlite_sessions, store)
        body = client.get(f"/ui/incidents/{incident_id}").text
        assert 'class="card summary st-recovered"' in body
        assert "What broke" in body
        assert "What Helix did" in body
        assert body.index("What broke") < body.index('class="fields"')

    def test_label_filter(self, client, sqlite_sessions, store):
        incident_id = _review_pending(sqlite_sessions, store)
        body = client.get(f"/ui/incidents/{incident_id}").text
        assert ui._label("review_pending") == "review pending"
        assert ">review pending<" in body
        assert ">review_pending<" not in body
        assert "st-review_pending" in body  # class keeps the raw value


class TestLongParams:
    def _page(self, client, sqlite_sessions, store, params) -> str:
        incident_id, _deps = _run_to_escalated(sqlite_sessions, store)
        with sqlite_sessions() as session:
            for cand in session.scalars(select(models.Candidate)):
                cand.params = params
            session.commit()
        return client.get(f"/ui/incidents/{incident_id}").text

    def test_long_params_collapse(self, client, sqlite_sessions, store):
        body = self._page(client, sqlite_sessions, store, {"content": "x" * 900})
        assert body.count("<summary>params</summary>") == 2  # decision + candidates table

    def test_short_params_stay_inline(self, client, sqlite_sessions, store):
        body = self._page(client, sqlite_sessions, store, {"path": "a.yaml"})
        assert "<summary>params</summary>" not in body
        assert "a.yaml" in body
