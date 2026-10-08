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


def test_sync_returns_1_on_failed_outbox(monkeypatch, capsys):
    """helix sync returns 1 when drain summary has failed > 0."""
    from helix import cli

    def fake_drain(session, limit):
        return {"sent": 0, "skipped": 0, "failed": 1, "pending": 0}

    monkeypatch.setattr("integrations.outbox.drain_outbox", fake_drain)
    rc = cli.main(["sync"])
    assert rc == 1
    out = capsys.readouterr()
    assert "outbox drain:" in out.out
    assert "1 failed" in out.out


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


def test_review_returns_1_on_error(env_db, monkeypatch, capsys):
    """helix review returns 1 when resumed run has result.error set."""
    from agent.runner import RunResult
    from helix import cli
    from state.models import RunStatus

    _db, env = env_db
    incident_id = _incident(env_db)

    # Mock run_incident to return an error
    def fake_run(iid):
        return RunResult(
            incident_id=iid,
            status="failed",
            run_status=RunStatus.FAILED,
            error="simulated run failure",
        )

    monkeypatch.setattr("agent.runner.run_incident", fake_run)
    # Update environment for cli.main
    import os

    old_env = os.environ.copy()
    os.environ.update(env)
    try:
        rc = cli.main(["review", incident_id, "approve", "--actor", "bob"])
        assert rc == 1
        out = capsys.readouterr()
        assert "run failed" in out.err
    finally:
        os.environ.clear()
        os.environ.update(old_env)


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


def test_daemons_turn_sigterm_into_a_clean_stop():
    """`docker stop`, systemd and plain `kill` send SIGTERM, and a process
    started in the background by a non-interactive shell ignores SIGINT.
    Before this, both daemons died on SIGTERM with no cleanup - a consumer
    killed mid-run would leave its incident stuck `running` for
    `helix reclaim`. Now SIGTERM raises KeyboardInterrupt, which `helix
    consume` handles by finishing the run in progress."""
    import os
    import signal

    from helix import cli

    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # as a background job inherits it
        cli._stop_cleanly_on_signals()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with pytest.raises(KeyboardInterrupt):
                os.kill(os.getpid(), sig)
                signal.pthread_sigmask(signal.SIG_BLOCK, [])  # let the handler run
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def test_watch_github_publishes_pr_activity_not_existing_prs(monkeypatch, capsys):
    """`helix watch --source github` polls open PRs: those open at start
    are the baseline, a new one is published as pr.opened."""
    from helix import cli

    listings = [
        [{"number": 1, "user": {"login": "a"}, "head": {"ref": "x", "sha": "1" * 40}}],
        [
            {"number": 1, "user": {"login": "a"}, "head": {"ref": "x", "sha": "1" * 40}},
            {"number": 2, "user": {"login": "dana"}, "head": {"ref": "feature/y", "sha": "2" * 40}},
        ],
    ]

    class FakeGitHub:
        def list_open_pull_requests_raw(self):
            return listings.pop(0)

    sent = []

    class FakeProducer:
        def publish(self, event):
            sent.append(event)

        def flush(self, timeout=10.0):
            return 0

    monkeypatch.setattr("integrations.github.client.GitHubEvidenceClient", FakeGitHub)
    monkeypatch.setattr("events.broker.EventProducer", FakeProducer)
    seen = {}

    def fake_run_watch(fetch, publish, **kwargs):
        # Mirrors run_watch: after_publish runs once a poll's events are
        # published. The PR tracker saves its view there; without it the
        # first listing stays the baseline forever and no PR is announced.
        seen.update(kwargs)
        for _ in range(2):
            for event in fetch():
                publish(event)
            if kwargs.get("after_publish"):
                kwargs["after_publish"]()
        return 2

    monkeypatch.setattr("events.watch.run_watch", fake_run_watch)
    assert cli.main(["watch", "--source", "github"]) == 0
    assert seen["polls"] == 1  # a PR event is a fact, not a condition to confirm
    assert seen["after_publish"] is not None  # the tracker is saved after publishing
    assert [(str(e.event_type), e.metadata["pr"], e.actor) for e in sent] == [
        ("pr.opened", 2, "dana")
    ]
    assert "pr.opened" in capsys.readouterr().out


def test_slack_review_passes_timeout_to_record_decision(monkeypatch):
    """Slack review button clicks pass timeout_s when calling record_decision."""
    import uuid

    from fastapi import BackgroundTasks
    from sqlalchemy import create_engine

    from helix import api
    from state import models
    from state import reviews as reviews_module
    from state.db import session_scope

    # Create a test review request
    tmp_db = Path(tempfile.mkdtemp(prefix="helix-slack-"))
    db_path = tmp_db / "state.db"
    engine = create_engine(f"sqlite:///{db_path}")
    models.Base.metadata.create_all(engine)

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
        review = models.ReviewRequest(
            incident_id=incident.id,
            reviewer="bob",
            rationale="test",
            status=models.ApprovalStatus.PENDING,
        )
        session.add(review)
        session.flush()
        review_id = review.id

    # Mock record_decision to capture the call
    calls = []
    original_record = reviews_module.record_decision

    def capture_record_decision(session, review, approved, actor, note, timeout_s=None):
        calls.append({"timeout_s": timeout_s})
        return original_record(session, review, approved, actor, note, timeout_s=timeout_s)

    monkeypatch.setattr("state.reviews.record_decision", capture_record_decision)

    # Call handle_slack_interaction with a review button click
    payload = {
        "type": "block_actions",
        "user": {"id": "U123", "username": "testuser"},
        "actions": [{"action_id": f"helix_review_approve:{review_id}"}],
    }
    with session_scope(engine) as session:
        background = BackgroundTasks()
        api.handle_slack_interaction(payload, session, background)

    # Verify that timeout_s was passed (should be approval_timeout or slack_approval_timeout)
    assert len(calls) >= 1
    assert calls[0]["timeout_s"] is not None
    assert isinstance(calls[0]["timeout_s"], (int, float))


def test_slack_socket_acks_clicks_and_hands_them_to_the_shared_handler(monkeypatch):
    """`helix slack-socket` opens Slack's Socket Mode connection, acks each
    envelope, passes button clicks to the same handler the HTTP route uses,
    runs the queued resume, and reconnects when Slack recycles the socket."""
    import json

    from helix import cli

    monkeypatch.setenv("HELIX_SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setattr("integrations.slack.socket.open_connection_url", lambda token: "wss://x")
    click = {
        "type": "interactive",
        "envelope_id": "env-9",
        "payload": {"type": "block_actions", "actions": [{"action_id": "helix_review_approve:37"}]},
    }
    sent, handled, resumed = [], [], []

    class FakeSocket:
        def __init__(self, messages):
            self._messages = [json.dumps(m) for m in messages]

        def recv(self):
            return self._messages.pop(0)

        def send(self, data):
            sent.append(json.loads(data))

        def close(self):
            pass

    connections = [
        FakeSocket(
            [{"type": "hello"}, click, {"type": "disconnect", "reason": "refresh_requested"}]
        )
    ]

    def connect(url, **kwargs):
        if not connections:
            raise KeyboardInterrupt  # end the test after one reconnect attempt
        return connections.pop(0)

    monkeypatch.setattr("websocket.create_connection", connect)

    def fake_handle(payload, session, background):
        handled.append(payload["actions"][0]["action_id"])
        background.add_task(resumed.append, "INC-x")
        return {"ok": True}

    monkeypatch.setattr("helix.api.handle_slack_interaction", fake_handle)
    assert cli.main(["slack-socket"]) == 0
    assert sent == [{"envelope_id": "env-9"}]
    assert handled == ["helix_review_approve:37"]
    import time

    for _ in range(50):
        if resumed:
            break
        time.sleep(0.01)
    assert resumed == ["INC-x"]


def test_slack_socket_refuses_without_an_app_token(monkeypatch, capsys):
    from helix import cli

    monkeypatch.setenv("HELIX_SLACK_APP_TOKEN", "")
    assert cli.main(["slack-socket"]) == 2
    assert "HELIX_SLACK_APP_TOKEN" in capsys.readouterr().err


def test_slack_socket_stops_on_a_wrong_token_instead_of_retrying(monkeypatch, capsys):
    from helix import cli
    from integrations.slack.client import SlackError

    monkeypatch.setenv("HELIX_SLACK_APP_TOKEN", "xoxb-wrong-kind")

    def refuse(token):
        raise SlackError("slack apps.connections.open failed: not_allowed_token_type")

    monkeypatch.setattr("integrations.slack.socket.open_connection_url", refuse)
    assert cli.main(["slack-socket"]) == 2
    assert "not_allowed_token_type" in capsys.readouterr().err
