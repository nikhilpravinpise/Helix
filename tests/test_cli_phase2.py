"""Phase 2 CLI surface tests: `helix sync`, `helix reviews`,
`helix review`, `helix graph-sync` - run as real subprocesses against a
per-test SQLite DB (the same way tests/agent/test_runner.py exercises
`helix run`)."""

import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from state import models

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def env_db():
    """A throwaway SQLite DB plus the env a `helix` subprocess needs."""
    tmp = Path(tempfile.mkdtemp(prefix="helix-cli-"))
    db = tmp / "state.db"
    engine = create_engine(f"sqlite:///{db}")
    models.Base.metadata.create_all(engine)
    env = dict(os.environ)
    env["HELIX_DATABASE_URL"] = f"sqlite:///{db}"
    env["HELIX_WORK_PROVIDER"] = "memory"
    env["HELIX_GRAPH_ENABLED"] = "false"
    yield db, env
    engine.dispose()


def _helix(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "helix", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _incident(env_db) -> str:
    """A change-kind incident parked at review_pending."""
    db, _env = env_db

    from state.db import session_scope

    engine = create_engine(f"sqlite:///{db}")
    with session_scope(engine) as session:
        incident = models.Incident(
            incident_id=f"INC-{uuid.uuid4().hex[:8]}",
            kind=models.IncidentKind.CHANGE,
            service="payment-api",
            environment="sandbox",
            severity="high",
            status=models.IncidentStatus.REVIEW_PENDING,
            fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
            owner="alice",
        )
        session.add(incident)
        session.flush()
        session.add(
            models.ReviewRequest(
                incident_id=incident.id,
                reviewer="bob",
                rationale="registry default reviewer",
                status=models.ApprovalStatus.PENDING,
            )
        )
        session.flush()
        return incident.incident_id


def test_sync_drains_and_reports(env_db):
    db, env = env_db
    # Seed one pending outbox row; `helix sync` must drain it.
    from state.db import session_scope
    from state.machine import IncidentStatus

    engine = create_engine(f"sqlite:///{db}")
    with session_scope(engine) as session:
        incident = models.Incident(
            incident_id=f"INC-{uuid.uuid4().hex[:8]}",
            kind=models.IncidentKind.CHANGE,
            service="payment-api",
            environment="sandbox",
            severity="high",
            status=IncidentStatus.TRIAGED,
            fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
        )
        session.add(incident)
        session.flush()
        from integrations.lifecycle_sync import enqueue_for_transition

        enqueue_for_transition(session, incident, IncidentStatus.TRIAGED)

    proc = _helix(["sync"], env)
    assert proc.returncode == 0, proc.stderr
    assert "outbox drain:" in proc.stdout
    # The pending rows resolved to a terminal outbox state.
    with session_scope(engine) as session:
        pending = session.scalars(
            select(models.IntegrationOutbox).where(
                models.IntegrationOutbox.status == models.OutboxStatus.PENDING
            )
        ).all()
    assert pending == []


def test_reviews_lists_pending(env_db):
    db, env = env_db
    incident_id = _incident(env_db)
    proc = _helix(["reviews"], env)
    assert proc.returncode == 0, proc.stderr
    assert incident_id in proc.stdout
    assert "bob" in proc.stdout


def test_review_decision_records_and_resumes(env_db):
    db, env = env_db
    incident_id = _incident(env_db)
    proc = _helix(["review", incident_id, "approve", "--actor", "bob"], env)
    assert proc.returncode == 0, proc.stderr
    assert "approved" in proc.stdout

    from sqlalchemy.orm import Session

    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as session:
        review = session.scalar(select(models.ReviewRequest))
        assert review.status == models.ApprovalStatus.APPROVED
        assert review.decided_by == "bob"


def test_review_unknown_incident_is_clean_error(env_db):
    _db, env = env_db
    proc = _helix(["review", "INC-nope0000", "approve"], env)
    assert proc.returncode == 2
    assert "no such incident" in proc.stderr


def test_graph_sync_refuses_when_disabled(env_db):
    _db, env = env_db
    proc = _helix(["graph-sync"], env)
    assert proc.returncode == 2
    assert "HELIX_GRAPH_ENABLED" in proc.stderr


def test_graph_sync_with_memory_store(env_db):
    """graph-enabled without a reachable Neo4j: sync_graph must still
    complete if the store resolves - against no live Neo4j the command
    reports the unreachable store cleanly rather than crashing."""
    db, env = env_db
    env["HELIX_GRAPH_ENABLED"] = "true"
    env["HELIX_NEO4J_URI"] = "bolt://127.0.0.1:1"  # nothing listening
    proc = _helix(["graph-sync"], env)
    # Unreachable Neo4j -> rc 1 with a clear message, not a traceback.
    assert proc.returncode == 1
    assert "graph store unreachable" in proc.stderr


def test_consume_run_drives_each_new_incident_once(monkeypatch, capsys):
    """`helix consume --run` hands every incident a new event lands on to a
    single background worker, so the consumer keeps polling while a run
    takes minutes. Two events for one incident queue it once."""
    from events.ingest import IngestResult
    from helix import cli

    driven = []
    monkeypatch.setattr("agent.runner.run_incident", lambda iid: driven.append(iid))

    def fake_run_consumer(session_factory, **kwargs):
        hook = kwargs["on_ingested"]
        for iid in ("INC-a", "INC-a", "INC-b"):
            hook(IngestResult(incident_id=iid, created=True, duplicate=False, correlated=False))
        return 3

    monkeypatch.setattr("events.ingest.run_consumer", fake_run_consumer)
    assert cli.main(["consume", "--run"]) == 0
    assert sorted(set(driven)) == ["INC-a", "INC-b"]
    assert "INC-a" in capsys.readouterr().out


def test_consume_without_run_never_drives(monkeypatch):
    """Plain `helix consume` keeps D19's contract: it only records."""
    from helix import cli

    seen = {}

    def fake_run_consumer(session_factory, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr("events.ingest.run_consumer", fake_run_consumer)
    assert cli.main(["consume"]) == 0
    assert seen["on_ingested"] is None
    assert seen["follow"] is False


def test_watch_wires_the_cluster_feed_to_kafka(monkeypatch, capsys):
    """`helix watch` fetches the namespace's pods, adapts them, and hands
    confirmed problems to the Kafka producer - nothing else."""
    from helix import cli

    raw = {
        "items": [
            {
                "metadata": {"name": "payment-api-x", "uid": "u-9", "namespace": "payment-api"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {
                            "name": "payment-api",
                            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                            "restartCount": 2,
                        }
                    ],
                },
            }
        ]
    }

    class FakeClient:
        def list_pods_raw(self, namespace):
            assert namespace == "payment-api"
            return raw

    sent = []

    class FakeProducer:
        def publish(self, event):
            sent.append(event)

        def flush(self, timeout=10.0):
            return 0

    monkeypatch.setattr("integrations.kubernetes.client.KubernetesEvidenceClient", FakeClient)
    monkeypatch.setattr("events.broker.EventProducer", FakeProducer)
    seen = {}

    def fake_run_watch(fetch, publish, **kwargs):
        seen.update(kwargs)
        for event in fetch():
            publish(event)
        return 1

    monkeypatch.setattr("events.watch.run_watch", fake_run_watch)
    assert cli.main(["watch", "--interval", "5", "--polls", "2"]) == 0
    assert seen["interval"] == 5 and seen["polls"] == 2
    assert [e.metadata["waiting_reason"] for e in sent] == ["CrashLoopBackOff"]
    assert "pod.failed" in capsys.readouterr().out
