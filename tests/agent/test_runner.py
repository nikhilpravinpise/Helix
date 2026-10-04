"""Runner surface tests: `helix run` output and the intake -> run hooks
(CLI `--run`, API `?run=true`). Unit tier - the intake write is bound to
the test's SQLite store and the runner is monkeypatched where the hooks
call it."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fakes import K8S_SIGNAL, FakeLLM, make_deps
from sqlalchemy import select

from agent.runner import (
    IncidentNotFoundError,
    RunResult,
    reclaim_stale_run,
    run_incident,
    start_run,
)
from helix.cli import main as helix_main
from state import models
from state.intake import intake_signal
from state.machine import IncidentStatus


def _make_incident(sessions, signal: dict) -> str:
    with sessions() as session:
        return intake_signal(session, signal).incident_id


def _patch_intake_db(monkeypatch, sqlite_sessions) -> None:
    """`helix intake` resolves state.db.session_scope at call time - bind
    it to the test's SQLite engine so the unit test never touches
    Postgres."""
    monkeypatch.setattr("state.db.session_scope", lambda *a, **kw: sqlite_sessions())


class TestRunIncident:
    def test_unknown_incident_raises(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        with pytest.raises(IncidentNotFoundError):
            run_incident("INC-nonexistent", deps=deps)

    def test_run_result_shape(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.incident_id == incident_id
        # Unscripted FakeLLM: empty CandidateList -> planner escalates (1.6).
        assert result.status == IncidentStatus.ESCALATED
        assert result.run_status == models.RunStatus.TERMINAL
        assert result.hypothesis["claim"]
        assert result.error is None


class TestCliRun:
    def test_helix_run_prints_status_and_hypothesis(
        self, sqlite_sessions, store, capsys, monkeypatch
    ):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        # cli.py binds run_incident at call time inside _cmd_run.
        monkeypatch.setattr(
            "agent.runner.run_incident", lambda iid, **kw: run_incident(iid, deps=deps)
        )
        rc = helix_main(["run", incident_id])
        out = capsys.readouterr().out

        assert rc == 0
        assert incident_id in out
        assert "status=escalated" in out
        assert "hypothesis:" in out

    def test_helix_run_unknown_incident(self, sqlite_sessions, store, capsys, monkeypatch):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        monkeypatch.setattr(
            "agent.runner.run_incident", lambda iid, **kw: run_incident(iid, deps=deps)
        )
        rc = helix_main(["run", "INC-nope"])
        assert rc == 2
        assert "no such incident" in capsys.readouterr().err


class TestReclaimStaleRun:
    """Orphaned-run recovery (agent/runner.py::reclaim_stale_run): the
    explicit path that unsticks a workflow_runs row left `running` by a
    hard-killed process, without ever touching a run genuinely still
    executing."""

    def _running(self, sessions, incident_id: str) -> int:
        with sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            run = start_run(session, incident)
            return run.id

    def test_no_running_row_is_noop(self, sqlite_sessions, store):
        """An incident with no running row: reclaim is a no-op, not an
        error."""
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert reclaim_stale_run(session, incident, 3600.0) is None

    def test_fresh_run_not_reclaimed(self, sqlite_sessions, store):
        """A run started moments ago must never be reclaimed, even under
        a call - the guard exists precisely to prevent two concurrent
        runs on one incident."""
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        run_id = self._running(sqlite_sessions, incident_id)

        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert reclaim_stale_run(session, incident, 3600.0) is None

        with sqlite_sessions() as session:
            run = session.get(models.WorkflowRun, run_id)
            assert run.status == models.RunStatus.RUNNING
            assert run.stale_reason is None

    def test_stale_run_reclaimed(self, sqlite_sessions, store):
        """A run whose started_at is well past the timeout is declared
        stale, marked failed, and the reason is recorded on the row."""
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        run_id = self._running(sqlite_sessions, incident_id)
        with sqlite_sessions() as session:
            run = session.get(models.WorkflowRun, run_id)
            run.started_at = datetime.now(UTC) - timedelta(seconds=7200)

        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            reclaimed = reclaim_stale_run(session, incident, 3600.0)
            assert reclaimed is not None
            assert reclaimed.id == run_id
            assert reclaimed.status == models.RunStatus.FAILED
            assert reclaimed.ended_at is not None
            assert "HELIX_STALE_RUN_TIMEOUT=3600" in reclaimed.stale_reason

        # The row is no longer `running` - a second reclaim is a no-op,
        # and a fresh `helix run` can start (the guard is freed).
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert reclaim_stale_run(session, incident, 3600.0) is None
            start_run(session, incident)  # would raise RunConflictError if not freed

    def test_disabled_timeout_never_reclaims(self, sqlite_sessions, store):
        """timeout_s <= 0 disables reclamation outright, however stale
        the row."""
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        run_id = self._running(sqlite_sessions, incident_id)
        with sqlite_sessions() as session:
            run = session.get(models.WorkflowRun, run_id)
            run.started_at = datetime.now(UTC) - timedelta(days=1)

        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert reclaim_stale_run(session, incident, 0) is None


class TestCliReclaim:
    """`helix reclaim` - the CLI surface for orphaned-run recovery."""

    def test_unknown_incident(self, sqlite_sessions, store, capsys, monkeypatch):
        monkeypatch.setattr("state.db.session_scope", lambda *a, **kw: sqlite_sessions())
        rc = helix_main(["reclaim", "INC-nope"])
        assert rc == 2
        assert "no such incident" in capsys.readouterr().err

    def test_no_running_row_is_noop(self, sqlite_sessions, store, capsys, monkeypatch):
        monkeypatch.setattr("state.db.session_scope", lambda *a, **kw: sqlite_sessions())
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        rc = helix_main(["reclaim", incident_id])

        assert rc == 0
        assert "nothing to reclaim" in capsys.readouterr().out

    def test_fresh_run_refused(self, sqlite_sessions, store, capsys, monkeypatch):
        monkeypatch.setattr("state.db.session_scope", lambda *a, **kw: sqlite_sessions())
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            start_run(session, incident)

        rc = helix_main(["reclaim", incident_id, "--timeout", "3600"])

        assert rc == 2
        err = capsys.readouterr().err
        assert "not reclaimed" in err
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            running = session.scalar(
                select(models.WorkflowRun).where(models.WorkflowRun.incident_id == incident.id)
            )
            assert running.status == models.RunStatus.RUNNING

    def test_stale_run_reclaimed(self, sqlite_sessions, store, capsys, monkeypatch):
        monkeypatch.setattr("state.db.session_scope", lambda *a, **kw: sqlite_sessions())
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            run = start_run(session, incident)
            run.started_at = datetime.now(UTC) - timedelta(seconds=7200)

        rc = helix_main(["reclaim", incident_id, "--timeout", "3600"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "reclaimed" in out
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            run = session.scalar(
                select(models.WorkflowRun).where(models.WorkflowRun.incident_id == incident.id)
            )
            assert run.status == models.RunStatus.FAILED
            assert run.stale_reason is not None
            # The guard is freed: a fresh run can start.
            start_run(session, incident)


class TestIntakeRunHook:
    """`helix intake --run` drives the graph in-process; plain `helix
    intake` only records (the documented guard)."""

    def _signal_file(self, tmp_path) -> str:
        path = tmp_path / "signal.json"
        path.write_text(json.dumps(K8S_SIGNAL))
        return str(path)

    def test_intake_without_run_does_not_run(self, tmp_path, monkeypatch, capsys, sqlite_sessions):
        called = []
        monkeypatch.setattr("agent.runner.run_incident", lambda iid: called.append(iid))
        _patch_intake_db(monkeypatch, sqlite_sessions)

        assert helix_main(["intake", "--file", self._signal_file(tmp_path)]) == 0
        capsys.readouterr()
        assert called == []

    def test_intake_run_triggers_runner(self, tmp_path, monkeypatch, capsys, sqlite_sessions):
        called = []

        def fake_run(iid):
            called.append(iid)
            return RunResult(
                incident_id=iid,
                status="remediation_pending",
                run_status=models.RunStatus.TERMINAL,
            )

        monkeypatch.setattr("agent.runner.run_incident", fake_run)
        _patch_intake_db(monkeypatch, sqlite_sessions)

        assert helix_main(["intake", "--run", "--file", self._signal_file(tmp_path)]) == 0
        printed = capsys.readouterr().out.strip()

        assert called == [printed]
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == printed)
            )
            assert incident is not None
            assert incident.status == IncidentStatus.RECEIVED

    def test_intake_run_failure_does_not_fail_intake(
        self, tmp_path, monkeypatch, capsys, sqlite_sessions
    ):
        def boom(iid):
            raise RuntimeError("graph exploded")

        monkeypatch.setattr("agent.runner.run_incident", boom)
        _patch_intake_db(monkeypatch, sqlite_sessions)

        # The signal is already durable when the hook fires, so a run
        # failure is reported on stderr and intake still succeeds.
        assert helix_main(["intake", "--run", "--file", self._signal_file(tmp_path)]) == 0
        err = capsys.readouterr().err
        assert "run failed" in err


class TestApiRunHook:
    """`POST /intake/*?run=true` triggers the graph in-process after the
    signal is committed; plain POSTs only record."""

    @pytest.fixture
    def client(self, sqlite_engine):
        from fastapi.testclient import TestClient

        from helix.api import app, get_db_session
        from state.db import session_scope

        def override():
            with session_scope(sqlite_engine) as session:
                yield session

        app.dependency_overrides[get_db_session] = override
        with TestClient(app) as test_client:
            yield test_client
        app.dependency_overrides.clear()

    def test_api_intake_run_param_triggers_runner(self, client, monkeypatch):
        called = []

        def fake_run(iid):
            called.append(iid)
            return RunResult(
                incident_id=iid,
                status="remediation_pending",
                run_status=models.RunStatus.TERMINAL,
            )

        monkeypatch.setattr("agent.runner.run_incident", fake_run)

        response = client.post("/intake/kubernetes?run=true", json=K8S_SIGNAL)
        assert response.status_code == 200
        assert called == [response.json()["incident_id"]]

    def test_api_intake_plain_post_does_not_run(self, client, monkeypatch):
        called = []
        monkeypatch.setattr("agent.runner.run_incident", lambda iid: called.append(iid))
        response = client.post("/intake/kubernetes", json=K8S_SIGNAL)
        assert response.status_code == 200
        assert called == []

    def test_api_intake_run_failure_still_records(self, client, monkeypatch):
        def boom(iid):
            raise RuntimeError("graph exploded")

        monkeypatch.setattr("agent.runner.run_incident", boom)
        response = client.post("/intake/kubernetes?run=true", json=K8S_SIGNAL)
        assert response.status_code == 200
        assert response.json()["incident_id"].startswith("INC-")


def test_intake_run_is_scheduled_after_the_response_not_inside_it(sqlite_engine, monkeypatch):
    """A run takes minutes (the verify window alone is ~3), and every
    caller of intake is time-boxed: Jenkins' post-failure curl gives up
    after 10s, a cluster watcher must not stall on one pod. Running inside
    the request made `?run=true` fail for exactly the callers it exists
    for. The run is handed to a background task instead - scheduled, not
    yet started, when the endpoint returns."""
    from fastapi import BackgroundTasks

    from helix.api import post_intake_jenkins
    from state.db import session_scope
    from state.intake import Signal

    called = []
    monkeypatch.setattr("agent.runner.run_incident", lambda iid: called.append(iid))
    background = BackgroundTasks()
    signal = Signal(
        source="jenkins",
        service="payment-api",
        environment="sandbox",
        signature={"job": "payment-api", "stage": "Test"},
    )
    with session_scope(sqlite_engine) as session:
        result = post_intake_jenkins(signal, session, background, None, run=True)
    assert called == []
    assert len(background.tasks) == 1
    background.tasks[0].func(*background.tasks[0].args, **background.tasks[0].kwargs)
    assert called == [result.incident_id]


class TestRunUntilParked:
    """Unattended driving of the change lifecycle. `helix run` advances a
    change incident one stage per call - the benchmark's _drive_change and
    scripts/e2e_change_lifecycle.py both loop it - so a PR picked up by
    `helix consume --run` stopped at `triaged` and never reached a reviewer.
    run_until_parked keeps running while the incident advances on its own,
    and stops where it waits on something outside Helix."""

    def _scripted(self, monkeypatch, statuses):
        from agent import runner

        calls = []
        sequence = list(statuses)

        def fake(incident_id, **kwargs):
            calls.append(incident_id)
            return RunResult(
                incident_id=incident_id,
                status=sequence.pop(0),
                run_status=models.RunStatus.TERMINAL,
            )

        monkeypatch.setattr(runner, "run_incident", fake)
        return calls

    def test_it_runs_through_to_the_reviewer(self, monkeypatch):
        from agent.runner import run_until_parked

        calls = self._scripted(monkeypatch, ["triaged", "owner_assigned", "review_pending"])
        result = run_until_parked("INC-1")
        assert result.status == "review_pending"
        assert len(calls) == 3

    @pytest.mark.parametrize(
        "parked", ["review_pending", "awaiting_approval", "fix_required", "deploying", "recovered"]
    )
    def test_it_stops_where_helix_waits_on_the_outside(self, monkeypatch, parked):
        from agent.runner import run_until_parked

        calls = self._scripted(monkeypatch, [parked, "never-reached"])
        assert run_until_parked("INC-1").status == parked
        assert len(calls) == 1

    def test_it_stops_when_a_run_makes_no_progress(self, monkeypatch):
        """Never spin: a run that leaves the status where it was is a stop."""
        from agent.runner import run_until_parked

        calls = self._scripted(monkeypatch, ["triaged", "triaged", "never-reached"])
        assert run_until_parked("INC-1").status == "triaged"
        assert len(calls) == 2

    def test_it_is_bounded(self, monkeypatch):
        from agent.runner import run_until_parked

        calls = self._scripted(monkeypatch, ["triaged", "owner_assigned"] * 20)
        run_until_parked("INC-1", max_hops=4)
        assert len(calls) == 4
