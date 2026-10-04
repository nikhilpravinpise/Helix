"""Harness orchestration tests (subphase 1.11).

Unit tier: requirement checks, measurement helpers, and the honest skip
paths - no subprocess runs. The real end-to-end (`helix intake` + `helix
run` subprocesses against Postgres) is the integration tier.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from benchmark import catalog, harness
from benchmark.harness import (
    HarnessConfig,
    _collect,
    _first_observed_at,
    _ingest_via_events,
    _measure,
    check_requirements,
    run_benchmark,
    run_scenario,
)
from benchmark.scoring import OutcomeRecord
from state import models
from state.db import session_scope
from state.intake import Signal, intake_signal
from state.machine import TERMINAL_STATUSES, IncidentStatus, transition

SCENARIOS = catalog.load_scenarios()
BAD_URL = "postgresql+psycopg://helix:helix@127.0.0.1:59999/helix"


def test_check_requirements_flags_missing_postgres():
    unmet = check_requirements([], BAD_URL)
    assert "postgres" in unmet


def test_run_scenario_skips_on_unmet_requirements():
    """No Postgres -> an honest skip record, never a fabricated run."""
    cfg = HarnessConfig(arms=["A"], database_url=BAD_URL)
    record = run_scenario("ci-test-regression", SCENARIOS["ci-test-regression"], "A", cfg)
    assert record.skipped is not None
    assert "postgres" in record.skipped
    assert record.final_status is None


def test_arm_b_and_e_skip_without_llm_key(monkeypatch):
    monkeypatch.setattr(harness, "get_settings", lambda: SimpleNamespace(llm_api_key=None))
    cfg = HarnessConfig(arms=["B"], database_url=BAD_URL)
    record = run_scenario("ci-test-regression", SCENARIOS["ci-test-regression"], "B", cfg)
    assert record.skipped.startswith("arm B requires HELIX_LLM_API_KEY")


@pytest.mark.parametrize("arm", ["B", "C", "E"])
def test_a_key_in_dot_env_is_enough_to_run_an_llm_arm(monkeypatch, arm):
    """The key's documented home is .env, which only pydantic-settings
    loads - os.environ never sees it. Gating the skip on os.environ meant
    putting the key exactly where every document says to put it left all
    three LLM arms skipping, with a message naming a variable the user had
    already set. Reaching the requirements check at all is the assertion:
    it proves the arm was not refused for a missing key."""
    monkeypatch.delenv("HELIX_LLM_API_KEY", raising=False)  # .env only, not the environment
    monkeypatch.setattr(harness, "get_settings", lambda: SimpleNamespace(llm_api_key="gsk_x"))
    cfg = HarnessConfig(arms=[arm], database_url=BAD_URL)
    record = run_scenario("ci-test-regression", SCENARIOS["ci-test-regression"], arm, cfg)
    assert "requires HELIX_LLM_API_KEY" not in (record.skipped or "")


def test_forcing_fixture_mode_on_a_live_scenario_skips_rather_than_crashes():
    """`--mode fixture` used to read `evidence_fixture` unconditionally,
    and the live scenarios do not declare one - so the flag raised
    KeyError and killed the whole run, but only when the cluster happened
    to be up (with kind down these skip on requirements first). Checked
    before requirements, so this holds either way."""
    live = SCENARIOS["crashloop"]
    assert "evidence_fixture" not in live, "fixture-less live scenario expected"
    cfg = HarnessConfig(arms=["A"], mode="fixture", database_url=BAD_URL)
    record = run_scenario("crashloop", live, "A", cfg)
    assert record.skipped == "no evidence_fixture declared, cannot run in fixture mode"


def test_fixture_mode_still_runs_scenarios_that_declare_a_fixture():
    """The guard must not swallow the scenarios the flag exists for - they
    proceed to the normal requirements check."""
    cfg = HarnessConfig(arms=["A"], mode="fixture", database_url=BAD_URL)
    record = run_scenario("ci-test-regression", SCENARIOS["ci-test-regression"], "A", cfg)
    assert record.skipped is not None
    assert "postgres" in record.skipped


def test_malformed_scenario_is_a_catalog_defect_not_a_run():
    cfg = HarnessConfig(arms=["A"], database_url=BAD_URL)
    record = run_scenario("bogus", {"name": "bogus"}, "A", cfg)
    assert record.skipped == "catalog invalid"
    assert record.errors


def _execution_row(**overrides):
    """An executions row shaped exactly as `_collect` builds one.

    Deliberately no `detail` key: the Execution model has no such column,
    so a fabricated one here hid the reporting bug that printed "no
    successful executions row" underneath every passing check.
    """
    row = {
        "action": "open_fix_pr",
        "result": "success",
        "policy_decision": "allow_with_checks",
        "approval_id": None,
        "decided_by": None,
        "compensates_id": None,
        "origin": "fixture",
        "output": {
            "branch": "helix/INC-x",
            "files": ["sandbox/payment-api/tests/test_main.py"],
        },
    }
    return {**row, **overrides}


def test_measure_fixture_mode_checks_the_recorded_write():
    """Fixture measurement reads the recorded intended write: the right
    action succeeded and its output carries the declared content."""
    record = {"executions": [_execution_row()]}
    scenario = SCENARIOS["ci-test-regression"]
    checks = _measure(HarnessConfig(), scenario, record)
    assert all(c["ok"] for c in checks)
    assert any("executed_action" in c["check"] for c in checks)
    assert any("output_contains" in c["check"] for c in checks)


def test_measure_flags_a_wrong_write():
    record = {"executions": []}
    checks = _measure(HarnessConfig(), SCENARIOS["ci-test-regression"], record)
    assert any(not c["ok"] for c in checks)


def test_passing_executed_action_check_reports_the_row_it_matched():
    """A passing check must not carry the not-found string as evidence -
    the committed arm-A report did exactly that for all 12 scenarios."""
    record = {"executions": [_execution_row()]}
    checks = _measure(HarnessConfig(), SCENARIOS["ci-test-regression"], record)
    executed = next(c for c in checks if c["check"].startswith("executed_action"))
    assert executed["ok"]
    assert "no successful executions row" not in executed["detail"]
    assert "open_fix_pr" in executed["detail"]
    assert "result=success" in executed["detail"]


def test_failing_executed_action_check_says_the_row_is_missing():
    checks = _measure(HarnessConfig(), SCENARIOS["ci-test-regression"], {"executions": []})
    executed = next(c for c in checks if c["check"].startswith("executed_action"))
    assert not executed["ok"]
    assert executed["detail"] == "no successful executions row"


def test_executed_action_ignores_failed_and_compensating_rows():
    """Only a successful, non-compensating row counts - and when none
    qualifies the detail must say so rather than describe a rejected row."""
    record = {
        "executions": [
            _execution_row(result="failure"),
            _execution_row(compensates_id=7),
        ]
    }
    checks = _measure(HarnessConfig(), SCENARIOS["ci-test-regression"], record)
    executed = next(c for c in checks if c["check"].startswith("executed_action"))
    assert not executed["ok"]
    assert executed["detail"] == "no successful executions row"


def _sqlite_url(tmp_path) -> str:
    """A file-backed SQLite URL, not `sqlite://` in-memory - the harness's
    `_ingest_via_events` and this test's own read-back each open their own
    engine, and only a real file gives them the same database."""
    return f"sqlite:///{tmp_path / 'event-path.db'}"


class TestIngestViaEvents:
    """Unit tier for the event-path intake helper (subphase E7): no
    subprocess, no Postgres - a file-backed SQLite engine exercises the
    same `events.ingest.ingest_event` call the real harness makes."""

    def test_ingests_a_classifiable_signal_and_opens_an_incident(self, tmp_path):
        db_url = _sqlite_url(tmp_path)
        engine = create_engine(db_url)
        models.Base.metadata.create_all(engine)

        incident_id, skip = _ingest_via_events(SCENARIOS["ci-test-regression"], db_url)

        assert skip is None
        assert incident_id is not None
        with Session(engine) as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert incident is not None
            # The canonical event landed (E7's actual deliverable): the
            # vendor event ingest_event recorded, plus the `incident.created`
            # lifecycle event `machine.record_creation` always emits on a
            # new incident (E5) - both source paths get one of those.
            event_types = {r.event_type for r in incident.event_records}
            assert event_types == {"build.failed", "incident.created"}
            # No Signal row - the event path writes nothing beyond what
            # ingest_event itself produces. `agent/graph.py`'s
            # `_signal_from_events` is what reads this incident's diagnosis
            # input back out; see tests/agent/test_graph.py::TestSignalFallback.
            assert incident.signals == []

    def test_unclassifiable_signal_is_an_honest_skip_not_a_fabricated_event(self, tmp_path):
        db_url = _sqlite_url(tmp_path)
        engine = create_engine(db_url)
        models.Base.metadata.create_all(engine)

        scenario = {
            "signal": {
                "source": "kubernetes",
                "service": "payment-api",
                "environment": "sandbox",
                "signature": {},  # no waiting_reason/reason/symptom -> no rule matches
                "raw": {},
            }
        }
        incident_id, skip = _ingest_via_events(scenario, db_url)

        assert incident_id is None
        assert skip is not None
        assert "unclassifiable" in skip
        with Session(engine) as session:
            assert session.scalar(select(models.Incident)) is None

    def test_invalid_signal_is_an_honest_skip(self, tmp_path):
        db_url = _sqlite_url(tmp_path)
        engine = create_engine(db_url)
        models.Base.metadata.create_all(engine)

        incident_id, skip = _ingest_via_events({"signal": {"source": "kubernetes"}}, db_url)

        assert incident_id is None
        assert skip is not None
        assert "invalid signal" in skip


def test_change_flow_skips_on_unmet_requirements_before_ingesting():
    """Change scenarios get the same honest requirements gate - no events
    are ingested against a dead database."""
    cfg = HarnessConfig(arms=["A"], database_url=BAD_URL)
    record = run_scenario("org-known-owner-change", SCENARIOS["org-known-owner-change"], "A", cfg)
    assert record.skipped is not None
    assert "postgres" in record.skipped


def test_change_flow_arm_d_needs_the_llm_key(monkeypatch):
    """Arm D reasons through the LLM like B/C/E - no key is an honest
    skip, not a failed run."""
    monkeypatch.setattr(harness, "get_settings", lambda: SimpleNamespace(llm_api_key=None))
    cfg = HarnessConfig(arms=["D"], database_url=BAD_URL)
    record = run_scenario("org-known-owner-change", SCENARIOS["org-known-owner-change"], "D", cfg)
    assert record.skipped.startswith("arm D requires HELIX_LLM_API_KEY")


def test_run_scenario_event_path_skips_on_unmet_requirements_before_ingesting():
    """The requirements gate runs before intake, whichever path is
    configured - an event-path run with no Postgres must skip exactly like
    the direct path, never attempt to ingest against a dead database."""
    cfg = HarnessConfig(arms=["A"], database_url=BAD_URL, path="event")
    record = run_scenario("ci-test-regression", SCENARIOS["ci-test-regression"], "A", cfg)
    assert record.skipped is not None
    assert "postgres" in record.skipped


def test_run_benchmark_records_the_configured_path():
    """The report's top-level `path` field is what `benchmark.compare`
    reads to tell a direct-path report from an event-path one."""
    cfg = HarnessConfig(arms=["A"], scenarios=["does-not-exist"], path="event")
    report = run_benchmark(cfg)
    assert report["path"] == "event"
    assert report["mode"] == "auto"
    assert report["outcomes"] == []


def test_run_benchmark_default_path_is_direct():
    cfg = HarnessConfig(arms=["A"], scenarios=["does-not-exist"])
    report = run_benchmark(cfg)
    assert report["path"] == "direct"


class TestCrossArmIsolation:
    """Reproduces, and proves the fix for, the contamination mode in
    docs/benchmark/methodology.md: `--arm B --arm C` run in one invocation
    let arm C's identical signal attach to arm B's still-open (`verifying`)
    incident, so arm C's scored result was actually arm B's.

    Arms B/C/E need HELIX_LLM_API_KEY, unavailable here, so `run_scenario`
    is replaced with a fake that drives the REAL dedup path
    (`state.intake.intake_signal`) and the REAL state machine
    (`state.machine.transition`) directly - the same functions `helix
    intake`/`helix run` would call - and simulates arm B's actual documented
    end state (parked at `verifying`, which is not terminal) by walking
    real, validated transition edges. `run_benchmark`'s arm loop and
    `harness._close_arm_incidents` are exercised for real; nothing in
    `state/intake.py` is touched, weakened, or special-cased.
    """

    SIGNAL = {
        "source": "kubernetes",
        "service": "payment-api",
        "environment": "sandbox",
        "severity": "medium",
        "signature": {"waiting_reason": "CrashLoopBackOff", "container": "payment-api"},
        "raw": {},
    }

    def _fake_run_scenario(self, name, scenario, arm, cfg):
        """Stand-in for the LLM-requiring arms: intakes the identical
        signal through the real dedup path and, for arm B, drives the
        incident to its real designed park state (`verifying`)."""
        engine = create_engine(cfg.database_url)
        with session_scope(engine) as session:
            result = intake_signal(session, Signal.model_validate(self.SIGNAL))
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == result.incident_id)
            )
            if arm == "B":
                for status in (
                    IncidentStatus.DIAGNOSING,
                    IncidentStatus.REMEDIATION_PENDING,
                    IncidentStatus.VALIDATING,
                    IncidentStatus.EXECUTING,
                    IncidentStatus.VERIFYING,
                ):
                    transition(incident, status, cause="simulated arm B drive", session=session)
            final_status = IncidentStatus(incident.status).value
        record = OutcomeRecord(scenario=name, arm=arm)
        record.incident_id = result.incident_id
        record.final_status = final_status
        return record

    def test_arm_c_does_not_inherit_arm_bs_open_incident(self, tmp_path, monkeypatch):
        db_url = f"sqlite:///{tmp_path / 'isolation.db'}"
        engine = create_engine(db_url)
        models.Base.metadata.create_all(engine)

        monkeypatch.setattr(harness, "run_scenario", self._fake_run_scenario)
        cfg = HarnessConfig(arms=["B", "C"], scenarios=["ci-test-regression"], database_url=db_url)
        report = run_benchmark(cfg)

        b_outcome, c_outcome = report["outcomes"]
        assert b_outcome["final_status"] == "verifying"
        assert b_outcome["incident_id"] != c_outcome["incident_id"], (
            "arm C attached to arm B's still-open incident - cross-arm contamination"
        )
        # Structural proof, not just "different id": arm B's incident must
        # now be terminal, so a THIRD arm's identical signal cannot attach
        # to it either.
        with Session(engine) as session:
            b_incident = session.scalar(
                select(models.Incident).where(
                    models.Incident.incident_id == b_outcome["incident_id"]
                )
            )
            assert IncidentStatus(b_incident.status) in TERMINAL_STATUSES

    def test_single_arm_never_closes_its_own_incidents(self, tmp_path, monkeypatch):
        """Rule 4: a single-arm invocation must behave exactly as today -
        `_close_arm_incidents` must never run when there is no later arm to
        protect against, so a lone arm's incident is left exactly as its
        own drive loop left it."""
        db_url = f"sqlite:///{tmp_path / 'single-arm.db'}"
        engine = create_engine(db_url)
        models.Base.metadata.create_all(engine)

        monkeypatch.setattr(harness, "run_scenario", self._fake_run_scenario)
        cfg = HarnessConfig(arms=["B"], scenarios=["ci-test-regression"], database_url=db_url)
        report = run_benchmark(cfg)

        (b_outcome,) = report["outcomes"]
        assert b_outcome["final_status"] == "verifying"
        with Session(engine) as session:
            incident = session.scalar(
                select(models.Incident).where(
                    models.Incident.incident_id == b_outcome["incident_id"]
                )
            )
            # Untouched: still parked at verifying, not force-closed.
            assert IncidentStatus(incident.status) == IncidentStatus.VERIFYING


# --- Change flow (subphase 2.9/2.11) ------------------------------------------


def _file_db(tmp_path) -> tuple[str, "object"]:
    """A file-backed SQLite URL + engine - the harness's subprocesses
    and this test's own read-back each open their own engine, and only a
    real file gives them the same database."""
    url = f"sqlite:///{tmp_path / 'change.db'}"
    engine = create_engine(url)
    models.Base.metadata.create_all(engine)
    return url, engine


class TestIngestDeclaredEvents:
    """Unit tier for change-flow intake: real `events.ingest.ingest_event`
    against file SQLite, no subprocess."""

    def test_single_pr_event_opens_a_change_incident(self, tmp_path):
        db_url, engine = _file_db(tmp_path)
        results, error = harness._ingest_declared_events(
            SCENARIOS["org-known-owner-change"], db_url
        )
        assert error is None
        assert len(results) == 1 and results[0]["created"] is True
        with Session(engine) as session:
            incident = session.scalar(select(models.Incident))
            assert incident.kind == models.IncidentKind.CHANGE
            assert incident.status == IncidentStatus.RECEIVED
            assert {r.event_type for r in incident.event_records} == {
                "pr.opened",
                "incident.created",
            }

    def test_duplicate_events_open_two_incidents(self, tmp_path):
        """The second pr.opened lands outside the correlation window via
        offset_seconds and must get its own incident - the duplicate
        scenario's whole premise."""
        db_url, engine = _file_db(tmp_path)
        results, error = harness._ingest_declared_events(SCENARIOS["org-duplicate-change"], db_url)
        assert error is None
        assert len(results) == 2
        assert results[0]["created"] and results[1]["created"]
        assert results[0]["incident_id"] != results[1]["incident_id"]
        with Session(engine) as session:
            assert session.scalars(select(models.Incident)).all().__len__() == 2

    def test_invalid_event_type_is_an_honest_error(self, tmp_path):
        db_url, _ = _file_db(tmp_path)
        scenario = {
            "events": [
                {
                    "type": "pr.nuked",
                    "source": "github",
                    "service": "payment-api",
                    "environment": "sandbox",
                }
            ]
        }
        results, error = harness._ingest_declared_events(scenario, db_url)
        assert error is not None
        assert "ingest" in error


class TestChangeDriveInProcess:
    """`_drive_change` with `harness._helix` faked to call the real
    `run_incident` graph in-process - the coordinator's transitions, the
    scripted reviewer, the event injections and the integration settle
    are all real; only the subprocess boundary is skipped."""

    def _fake_helix(self, deps):
        from agent.runner import run_incident

        def _fake(args, env, stdin_text=None):
            assert args[0] == "run"
            run_incident(args[1], deps=deps, arm="A")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        return _fake

    def test_known_owner_change_reaches_recovered(self, tmp_path, monkeypatch, store):
        import functools

        from fakes import FakeLLM, make_deps

        db_url, engine = _file_db(tmp_path)
        sessions = functools.partial(session_scope, engine)
        deps, _ = make_deps(sessions, FakeLLM(), "cluster/crashloop", store, database_url=db_url)
        monkeypatch.setattr(harness, "_helix", self._fake_helix(deps))

        scenario = SCENARIOS["org-known-owner-change"]
        record = run_scenario(
            "org-known-owner-change",
            scenario,
            "A",
            HarnessConfig(arms=["A"], database_url=db_url),
        )
        assert record.skipped is None
        assert record.errors == []
        assert record.final_status == "recovered"
        assert record.owner == "alice"
        assert record.reviews[0]["reviewer"] == "bob"
        assert record.reviews[0]["status"] == "approved"
        to = [t["to"] for t in record.transitions]
        assert to == [
            "received",
            "triaged",
            "owner_assigned",
            "review_pending",
            "approved",
            "deploying",
            "recovered",
        ]
        assert record.work_items[0]["status"] == "done"
        assert all(c["ok"] for c in record.measured)

    def test_reject_then_rereview_uses_a_different_reviewer(self, tmp_path, monkeypatch, store):
        import functools

        from fakes import FakeLLM, make_deps

        db_url, engine = _file_db(tmp_path)
        sessions = functools.partial(session_scope, engine)
        deps, _ = make_deps(sessions, FakeLLM(), "cluster/crashloop", store, database_url=db_url)
        monkeypatch.setattr(harness, "_helix", self._fake_helix(deps))

        scenario = SCENARIOS["org-reject-rereview"]
        record = run_scenario(
            "org-reject-rereview",
            scenario,
            "A",
            HarnessConfig(arms=["A"], database_url=db_url),
        )
        assert record.skipped is None
        assert record.errors == []
        assert record.final_status == "recovered"
        assert [r["reviewer"] for r in record.reviews] == ["bob", "carol"]
        assert [r["status"] for r in record.reviews] == ["rejected", "approved"]
        to = [t["to"] for t in record.transitions]
        assert to == [
            "received",
            "triaged",
            "owner_assigned",
            "review_pending",
            "rejected",
            "fix_required",
            "review_pending",
            "approved",
            "deploying",
            "recovered",
        ]

    def test_missing_owner_escalates_without_guessing(self, tmp_path, monkeypatch, store):
        import functools

        from fakes import FakeLLM, make_deps

        db_url, engine = _file_db(tmp_path)
        sessions = functools.partial(session_scope, engine)
        deps, _ = make_deps(sessions, FakeLLM(), "cluster/crashloop", store, database_url=db_url)
        monkeypatch.setattr(harness, "_helix", self._fake_helix(deps))

        scenario = SCENARIOS["org-missing-owner"]
        record = run_scenario(
            "org-missing-owner",
            scenario,
            "A",
            HarnessConfig(arms=["A"], database_url=db_url),
        )
        assert record.final_status == "escalated"
        assert record.owner is None
        assert [t["to"] for t in record.transitions] == ["received", "escalated"]
        # The escalation is tracked work - one blocked item, not zero.
        assert [w["status"] for w in record.work_items] == ["blocked"]

    def test_duplicate_change_escalates_with_duplicate_edge(self, tmp_path, monkeypatch, store):
        import functools

        from fakes import FakeLLM, make_deps

        db_url, engine = _file_db(tmp_path)
        sessions = functools.partial(session_scope, engine)
        deps, _ = make_deps(sessions, FakeLLM(), "cluster/crashloop", store, database_url=db_url)
        monkeypatch.setattr(harness, "_helix", self._fake_helix(deps))

        scenario = SCENARIOS["org-duplicate-change"]
        record = run_scenario(
            "org-duplicate-change",
            scenario,
            "A",
            HarnessConfig(arms=["A"], database_url=db_url),
        )
        assert record.final_status == "escalated"
        assert record.extra_incident_ids, "the first event's incident is tracked"
        assert record.duplicate_of == record.extra_incident_ids[0]

    def test_deploy_failure_hands_off_to_the_recovery_loop(self, tmp_path, monkeypatch, store):
        """The cross-system path end to end: change lifecycle through
        `deploying`, then the injected deployment.failed event hands the
        SAME incident to the Ops loop, which diagnoses the fixture's
        docker-build-failure signature and remediates via open_fix_pr."""
        import functools

        from fakes import FakeLLM, make_deps

        db_url, engine = _file_db(tmp_path)
        sessions = functools.partial(session_scope, engine)
        deps, _ = make_deps(
            sessions,
            FakeLLM(),
            "ci/docker-build-failure",
            store,
            database_url=db_url,
        )
        monkeypatch.setattr(harness, "_helix", self._fake_helix(deps))

        scenario = SCENARIOS["xsys-pr-deploy-failure"]
        record = run_scenario(
            "xsys-pr-deploy-failure",
            scenario,
            "A",
            HarnessConfig(arms=["A"], database_url=db_url),
        )
        assert record.skipped is None
        assert record.errors == []
        assert record.final_status == "recovered"
        assert record.owner == "bob"
        assert record.reviews[0]["reviewer"] == "carol"  # bob authored the PR
        assert record.selected_action == "open_fix_pr"
        assert record.decision_evidence["candidates_total"] >= 1
        to = [t["to"] for t in record.transitions]
        assert to == [
            "received",
            "triaged",
            "owner_assigned",
            "review_pending",
            "approved",
            "deploying",
            "diagnosing",
            "remediation_pending",
            "validating",
            "executing",
            "verifying",
            "recovered",
        ]

    def test_review_timeout_parks_honestly(self, tmp_path, monkeypatch, store):
        """A scripted `timeout` never decides - the incident stays at
        review_pending and the drive reports it parked, not failed."""
        import functools

        from fakes import FakeLLM, make_deps

        db_url, engine = _file_db(tmp_path)
        sessions = functools.partial(session_scope, engine)
        deps, _ = make_deps(sessions, FakeLLM(), "cluster/crashloop", store, database_url=db_url)
        monkeypatch.setattr(harness, "_helix", self._fake_helix(deps))

        scenario = dict(SCENARIOS["org-known-owner-change"])
        scenario["name"] = "timeout-test"  # validation binds name to file stem
        scenario["review_decision"] = "timeout"
        scenario["expected_work_item_status"] = "in_progress"
        scenario["expected_transitions"] = [
            "received",
            "triaged",
            "owner_assigned",
            "review_pending",
        ]
        scenario["expected_final_state"] = {"status": "review_pending"}
        record = run_scenario(
            "timeout-test",
            scenario,
            "A",
            HarnessConfig(arms=["A"], database_url=db_url),
        )
        assert record.final_status == "review_pending"
        assert record.reviews[0]["status"] == "pending"
        assert record.reviews[0]["decided_by"] is None


class TestChangeFlowEndToEnd:
    """Real `uv run helix` subprocesses against file-backed SQLite - the
    change lifecycle driven exactly as the operator CLI drives it. Not
    marked `integration`: nothing here needs live infrastructure (the DB
    is a local file, the work provider is memory), so these run in the
    default suite - a benchmark claim without a running E2E is not
    evidence."""

    def test_known_owner_change_recovers_via_real_cli(self, tmp_path):
        db_url, engine = _file_db(tmp_path)
        scenario = SCENARIOS["org-known-owner-change"]
        record = run_scenario(
            "org-known-owner-change",
            scenario,
            "A",
            HarnessConfig(arms=["A"], database_url=db_url),
        )
        assert record.skipped is None
        assert record.errors == []
        assert record.final_status == "recovered"
        assert record.owner == "alice"
        assert record.reviews[0]["reviewer"] == "bob"
        assert record.work_items[0]["status"] == "done"
        assert all(c["ok"] for c in record.measured)
        # The durable rows are readable straight off the SQLite file.
        with Session(engine) as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == record.incident_id)
            )
            assert incident is not None
            assert incident.owner == "alice"
            assert incident.review_requests[0].status == models.ApprovalStatus.APPROVED

    def test_missing_owner_escalates_via_real_cli(self, tmp_path):
        db_url, _ = _file_db(tmp_path)
        scenario = SCENARIOS["org-missing-owner"]
        record = run_scenario(
            "org-missing-owner",
            scenario,
            "A",
            HarnessConfig(arms=["A"], database_url=db_url),
        )
        assert record.skipped is None
        assert record.final_status == "escalated"
        assert record.owner is None


@pytest.mark.integration
class TestHarnessEndToEnd:
    """Real `helix intake`/`helix run` subprocesses against the compose
    Postgres (D9 integration tier): the fixture-mode arm-A path through
    the actual CLI, not an in-process shortcut."""

    def test_fixture_arm_a_recovers_through_the_real_cli(self):
        cfg = HarnessConfig(arms=["A"], scenarios=["ci-test-regression"])
        record = run_scenario("ci-test-regression", SCENARIOS["ci-test-regression"], "A", cfg)
        assert record.skipped is None
        assert record.final_status == "recovered"
        assert record.selected_action == "open_fix_pr"
        # The durable record exists: a successful fixture write row and a
        # passing verification row, both readable back from Postgres.
        assert any(
            e["action"] == "open_fix_pr" and e["result"] == "success" and e["origin"] == "fixture"
            for e in record.executions
        )
        assert any(v["outcome"] == "pass" for v in record.verifications)
        assert record.recovered_at is not None

    def test_collect_reads_the_durable_record(self):
        from helix.config import get_settings

        cfg = HarnessConfig(arms=["A"], scenarios=["ci-test-regression"])
        record = run_scenario("ci-test-regression", SCENARIOS["ci-test-regression"], "A", cfg)
        assert record.incident_id is not None
        collected = _collect(get_settings().database_url, record.incident_id)
        assert collected["final_status"] == "recovered"

    def test_fixture_arm_a_recovers_through_the_event_path(self):
        """The same scenario as the direct-path test above, but intake
        goes through `events.ingest.ingest_event` instead of `helix
        intake` - the E7 comparison's whole point is that this reaches the
        same outcome as the direct path."""
        cfg = HarnessConfig(arms=["A"], scenarios=["ci-test-regression"], path="event")
        record = run_scenario("ci-test-regression", SCENARIOS["ci-test-regression"], "A", cfg)
        assert record.skipped is None
        assert record.final_status == "recovered"
        assert record.selected_action == "open_fix_pr"
        assert any(
            e["action"] == "open_fix_pr" and e["result"] == "success" and e["origin"] == "fixture"
            for e in record.executions
        )
        assert any(v["outcome"] == "pass" for v in record.verifications)
        assert record.recovered_at is not None


class TestFirstObservedAt:
    """The MTTR clock's start (subphase E7).

    `_collect` derived `signal_at` from `incident.signals[-1]`, and an
    event-sourced incident has no signal row by design (D22), so MTTR came
    back null on every event-path run while `benchmark.compare` still
    reported NO REGRESSION - MTTR is not one of its gated metrics. These
    tests existed nowhere until the claim "every fix has a regression
    test" was checked and found false.
    """

    def _incident(self, session, **kw):
        incident = models.Incident(
            incident_id=kw.get("iid", "INC-clock"),
            fingerprint="fp",
            service="payment-api",
            environment="sandbox",
            severity="high",
            status=IncidentStatus.RECEIVED,
        )
        session.add(incident)
        session.flush()
        return incident

    def _event_row(self, incident_id, source, when, event_id):
        return models.EventRecord(
            incident_id=incident_id,
            event_id=event_id,
            event_type="pod.failed",
            source=source,
            service="payment-api",
            environment="sandbox",
            occurred_at=when,
            envelope={},
        )

    def test_a_signal_row_is_the_clock_start_when_one_exists(self, tmp_path):
        """The direct path is unchanged: it always has a signal row and
        must never reach the event fallback."""
        engine = create_engine(_sqlite_url(tmp_path))
        models.Base.metadata.create_all(engine)
        with Session(engine) as session:
            incident = self._incident(session)
            session.add(
                models.Signal(
                    incident_id=incident.id,
                    source="kubernetes",
                    environment="sandbox",
                    severity="high",
                    signature={},
                    raw={},
                )
            )
            session.flush()
            session.refresh(incident)
            assert _first_observed_at(incident) == incident.signals[-1].created_at.isoformat()

    def test_the_earliest_vendor_event_is_the_clock_start_without_a_signal_row(self, tmp_path):
        engine = create_engine(_sqlite_url(tmp_path))
        models.Base.metadata.create_all(engine)
        early = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
        late = datetime(2026, 9, 21, 10, 5, tzinfo=UTC)
        with Session(engine) as session:
            incident = self._incident(session)
            session.add(self._event_row(incident.id, "kubernetes", late, "evt_late"))
            session.add(self._event_row(incident.id, "jenkins", early, "evt_early"))
            session.flush()
            session.refresh(incident)
            observed = _first_observed_at(incident)
        assert observed is not None
        assert observed.startswith("2026-09-21T10:00")

    def test_helix_lifecycle_events_do_not_start_the_clock(self, tmp_path):
        """They are stamped when Helix reacted, not when the failure
        happened - starting there would measure from the response and
        flatter MTTR."""
        engine = create_engine(_sqlite_url(tmp_path))
        models.Base.metadata.create_all(engine)
        helix_at = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
        vendor_at = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
        with Session(engine) as session:
            incident = self._incident(session)
            session.add(self._event_row(incident.id, "helix", helix_at, "evt_helix"))
            session.add(self._event_row(incident.id, "kubernetes", vendor_at, "evt_vendor"))
            session.flush()
            session.refresh(incident)
            observed = _first_observed_at(incident)
        assert observed.startswith("2026-09-21T10:00"), "the helix event started the clock"

    def test_an_incident_with_neither_has_no_clock_start(self, tmp_path):
        engine = create_engine(_sqlite_url(tmp_path))
        models.Base.metadata.create_all(engine)
        with Session(engine) as session:
            incident = self._incident(session)
            session.refresh(incident)
            assert _first_observed_at(incident) is None


class TestClosePreexistingIncidents:
    """Cross-INVOCATION isolation (subphase E7).

    `_close_arm_incidents` isolates arms within one invocation. This is the
    other half: an incident left open by an earlier benchmark run, which
    intake dedup attaches to silently because the fingerprint is identical
    across invocations.

    It is not hypothetical. An arm-B smoke test left an incident at
    `verifying`; the next full ablation's arm A deduped onto it, inherited
    arm B's hypothesis and candidate, and reported the result as arm A's -
    scoring 0.83 instead of its invariable 1.00 on reasoning it never did.
    Caught only because arm A is the control and its number moved.
    """

    def _seed(self, url, scenario_name, status=IncidentStatus.VERIFYING):
        """An open incident carrying the fingerprint `scenario_name` produces."""
        from state.intake import fingerprint

        engine = create_engine(url)
        models.Base.metadata.create_all(engine)
        signal = Signal.model_validate(SCENARIOS[scenario_name]["signal"])
        with Session(engine) as session:
            incident = models.Incident(
                incident_id="INC-leftover",
                fingerprint=fingerprint(signal),
                service=signal.service,
                environment=signal.environment,
                severity=signal.severity,
                status=status,
            )
            session.add(incident)
            session.commit()
        return engine

    def test_an_incident_left_open_by_an_earlier_run_is_closed(self, tmp_path):
        url = _sqlite_url(tmp_path)
        engine = self._seed(url, "ci-test-regression")
        closed = harness._close_preexisting_incidents(url, SCENARIOS)
        assert closed == 1
        with Session(engine) as session:
            incident = session.scalar(select(models.Incident))
            assert incident.status in TERMINAL_STATUSES

    def test_an_already_terminal_incident_is_left_alone(self, tmp_path):
        url = _sqlite_url(tmp_path)
        self._seed(url, "ci-test-regression", status=IncidentStatus.RECOVERED)
        assert harness._close_preexisting_incidents(url, SCENARIOS) == 0

    def test_an_unrelated_incident_is_not_touched(self, tmp_path):
        """Scoped to fingerprints this run will actually produce, so it
        cannot close incidents that have nothing to do with the benchmark."""
        url = _sqlite_url(tmp_path)
        engine = create_engine(url)
        models.Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(
                models.Incident(
                    incident_id="INC-unrelated",
                    fingerprint="a-fingerprint-no-scenario-produces",
                    service="other-service",
                    environment="sandbox",
                    severity="high",
                    status=IncidentStatus.VERIFYING,
                )
            )
            session.commit()
        assert harness._close_preexisting_incidents(url, SCENARIOS) == 0
        with Session(engine) as session:
            assert session.scalar(select(models.Incident)).status is IncidentStatus.VERIFYING

    def test_an_empty_store_is_not_an_error(self, tmp_path):
        url = _sqlite_url(tmp_path)
        models.Base.metadata.create_all(create_engine(url))
        assert harness._close_preexisting_incidents(url, SCENARIOS) == 0


class TestLiveInjectionStartsFromBaseline:
    """Each live scenario must start from the committed manifests, not from
    whatever the previous scenario left behind. Only crashloop.sh reset
    the cluster itself, so after an arm without verify or compensate (B,
    C, D) left failed-rollout's bad image in place, missing-secret's new
    pod stopped at ImagePullBackOff instead of CreateContainerConfigError
    - having already deleted the secret - and oom-killed then could not
    start a pod at all. Two of arm B's six live scenarios measured the
    cluster, not the arm."""

    def _fake_run(self, calls, fail_on=None):
        def run(cmd, **kwargs):
            calls.append(cmd[-1])
            code = 1 if fail_on and cmd[-1].endswith(fail_on) else 0
            return SimpleNamespace(returncode=code, stderr="boom", stdout="")

        return run

    def test_reset_runs_before_the_fault_script(self, monkeypatch):
        calls = []
        monkeypatch.setattr(harness.subprocess, "run", self._fake_run(calls))
        assert harness._inject_live({"fault_script": "faults/missing-secret.sh"}) is None
        assert [c.rsplit("/", 1)[-1] for c in calls] == ["reset.sh", "missing-secret.sh"]

    def test_a_failed_reset_stops_before_injecting(self, monkeypatch):
        """Injecting into a cluster that could not be reset would measure
        the leftover state again - report it instead."""
        calls = []
        monkeypatch.setattr(harness.subprocess, "run", self._fake_run(calls, fail_on="reset.sh"))
        detail = harness._inject_live({"fault_script": "faults/missing-secret.sh"})
        assert detail is not None and "reset" in detail
        assert len(calls) == 1


def test_benchmark_runs_never_reach_live_work_tracking_or_chat(monkeypatch):
    """Every scenario shells out to `helix`, which reads the developer's
    .env - and once that held real Jira and Slack credentials, a full run
    would have filed a ticket and posted a thread per scenario per arm.
    The harness pins those integrations to the recording fakes itself."""
    monkeypatch.setenv("HELIX_WORK_PROVIDER", "jira")
    monkeypatch.setenv("HELIX_SLACK_BOT_TOKEN", "xoxb-real")
    env = harness._isolated_env()
    assert env["HELIX_WORK_PROVIDER"] == "memory"
    assert env["HELIX_SLACK_BOT_TOKEN"] == ""
    assert env["HELIX_NOTION_API_KEY"] == ""
    assert env["HELIX_SERVICE_REGISTRY"].endswith("benchmark/service_registry.yaml")


def test_the_harness_process_itself_is_pinned_too(monkeypatch):
    """_settle_integrations drains the outbox inside the harness process, so
    pinning only the subprocesses would still let it reach live Jira."""
    monkeypatch.setenv("HELIX_WORK_PROVIDER", "jira")
    cfg = HarnessConfig(arms=["A"], scenarios=["no-such-scenario"], database_url=BAD_URL)
    run_benchmark(cfg)
    import os

    assert os.environ["HELIX_WORK_PROVIDER"] == "memory"
