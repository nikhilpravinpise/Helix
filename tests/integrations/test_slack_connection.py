"""Slack uses the stored connection with .env as the fallback (future_plans.md
Stage 1, slice 4): the connected workspace's bot token and channel win over
HELIX_SLACK_BOT_TOKEN and HELIX_SLACK_DEFAULT_CHANNEL, an explicit payload
channel still wins over both, and the benchmark's isolation switches stored
connections off so it stays offline.
"""

import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from benchmark import harness
from integrations.outbox import _resolve_slack_for, drain_outbox
from integrations.slack import WebSlackClient
from state import connections, models
from state.machine import IncidentStatus

STORED = "xoxb-stored"
ENV_TOKEN = "xoxb-from-env"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "test connections key")
    monkeypatch.setenv("HELIX_SLACK_BOT_TOKEN", ENV_TOKEN)
    monkeypatch.setenv("HELIX_SLACK_DEFAULT_CHANNEL", "#from-env")
    monkeypatch.delenv("HELIX_CONNECTIONS_ENABLED", raising=False)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s
    engine.dispose()


def _connect(session, *, channel=None):
    connections.save_connection(
        session,
        "slack",
        account="Acme",
        scopes=["chat:write"],
        access_token=STORED,
        refresh_token=None,
        expires_at=None,
        settings={"channel": channel} if channel else {},
    )
    session.commit()


class _Slack:
    def __init__(self):
        self.channels: list[str] = []

    def post_message(self, channel, text, *, thread_ts=None, blocks=None):
        self.channels.append(channel)
        return {"ok": True, "ts": "1.1"}

    def update_message(self, channel, ts, text, *, blocks=None):
        return {"ok": True, "ts": ts}


def _post_channel(session, payload):
    incident = models.Incident(
        incident_id=f"INC-{uuid.uuid4().hex[:8]}",
        kind=models.IncidentKind("change"),
        service="payment-api",
        environment="sandbox",
        severity="high",
        status=IncidentStatus.RECEIVED,
        fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
    )
    session.add(incident)
    session.flush()
    session.add(
        models.IntegrationOutbox(
            incident_id=incident.id, integration="slack", op="notify", payload=payload
        )
    )
    session.flush()
    slack = _Slack()
    drain_outbox(session, slack=slack)
    assert session.scalars(select(models.IntegrationOutbox)).one().status == "sent"
    return slack.channels[0]


def _token(client):
    assert isinstance(client, WebSlackClient)
    return client._token


class TestClientToken:
    def test_stored_connection_token_beats_env(self, session):
        _connect(session)
        assert _token(_resolve_slack_for(session)) == STORED

    def test_no_stored_connection_uses_env_token(self, session):
        assert _token(_resolve_slack_for(session)) == ENV_TOKEN

    def test_neither_returns_none(self, session, monkeypatch):
        monkeypatch.setenv("HELIX_SLACK_BOT_TOKEN", "")
        assert _resolve_slack_for(session) is None

    def test_benchmark_isolation_ignores_stored_connection(self, session, monkeypatch):
        _connect(session)
        for name, value in harness._BENCHMARK_ISOLATION.items():
            monkeypatch.setenv(name, value)
        assert _resolve_slack_for(session) is None

    def test_store_read_error_falls_back_to_env_with_warning(self, session, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("no such table: connections")

        warnings = []
        monkeypatch.setattr(connections, "get_connection", boom)
        monkeypatch.setattr(
            connections,
            "get_logger",
            lambda: type("L", (), {"warning": lambda self, *a, **k: warnings.append((a, k))})(),
        )
        assert _token(_resolve_slack_for(session)) == ENV_TOKEN
        assert warnings and warnings[0][0] == ("connection_unreadable",)

    def test_missing_table_falls_back_to_env(self, monkeypatch):
        engine = create_engine("sqlite://")
        with Session(engine) as bare:
            assert _token(_resolve_slack_for(bare)) == ENV_TOKEN


class TestChannel:
    def test_stored_channel_used_when_payload_has_none(self, session):
        _connect(session, channel="C-stored")
        assert _post_channel(session, {}) == "C-stored"

    def test_payload_channel_beats_stored(self, session):
        _connect(session, channel="C-stored")
        assert _post_channel(session, {"channel": "#explicit"}) == "#explicit"

    def test_connection_without_channel_uses_env_default(self, session):
        _connect(session)
        assert _post_channel(session, {}) == "#from-env"

    def test_isolation_ignores_stored_channel(self, session, monkeypatch):
        _connect(session, channel="C-stored")
        monkeypatch.setenv("HELIX_CONNECTIONS_ENABLED", "false")
        assert _post_channel(session, {}) == "#from-env"
