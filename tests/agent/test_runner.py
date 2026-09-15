"""Runner surface tests: `helix run` output and the intake -> run hooks
(CLI `--run`, API `?run=true`). Unit tier - the intake write is bound to
the test's SQLite store and the runner is monkeypatched where the hooks
call it."""

import json

import pytest
from fakes import K8S_SIGNAL, FakeLLM, make_deps
from sqlalchemy import select

from agent.runner import IncidentNotFoundError, RunResult, run_incident
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
        assert result.status == IncidentStatus.REMEDIATION_PENDING
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
        assert "status=remediation_pending" in out
        assert "hypothesis:" in out

    def test_helix_run_unknown_incident(self, sqlite_sessions, store, capsys, monkeypatch):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        monkeypatch.setattr(
            "agent.runner.run_incident", lambda iid, **kw: run_incident(iid, deps=deps)
        )
        rc = helix_main(["run", "INC-nope"])
        assert rc == 2
        assert "no such incident" in capsys.readouterr().err


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
