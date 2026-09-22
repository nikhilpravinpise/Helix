"""Knowledge Agent context tests (subphase 2.8): the composed GraphRAG
context - E6 retrieval + event timeline + graph neighborhood - and the
contracts that make it honest: every line cites a record, and any
failure collapses to "" rather than failing the run.
"""

import functools
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from agent.knowledge_context import knowledge_context
from events.schemas import EventSource, EventType
from state import models
from state.db import session_scope


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def sessions(engine):
    return functools.partial(session_scope, engine)


class _Deps:
    """The two fields knowledge_context reads on deps: the session factory
    (timeline + retrieval) and llm (retrieval's rerank path - unused here
    since retrieval only runs when include_retrieval and prior incidents
    exist)."""

    def __init__(self, sessions):
        self.session_scope = sessions
        self.llm = None


def _event_record(session, service: str, event_type: EventType) -> models.EventRecord:
    incident = session.scalar(
        __import__("sqlalchemy").select(models.Incident).where(models.Incident.service == service)
    )
    if incident is None:
        incident = models.Incident(
            incident_id=f"INC-{uuid.uuid4().hex[:8]}",
            kind=models.IncidentKind.CHANGE,
            service=service,
            environment="sandbox",
            severity="high",
            status=models.IncidentStatus.RECEIVED,
            fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
        )
        session.add(incident)
        session.flush()
    envelope = {
        "schema_version": "1.0",
        "event_id": f"evt-{uuid.uuid4().hex[:10]}",
        "event_type": event_type.value,
        "source": EventSource.GITHUB.value,
        "timestamp": datetime.now(UTC).isoformat(),
        "service": service,
        "environment": "sandbox",
        "actor": "alice",
        "metadata": {"pr": 7, "branch": "feature/x"},
    }
    record = models.EventRecord(
        incident_id=incident.id,
        event_id=envelope["event_id"],
        event_type=event_type.value,
        source=envelope["source"],
        service=service,
        environment="sandbox",
        occurred_at=datetime.now(UTC),
        envelope=envelope,
    )
    session.add(record)
    session.flush()
    return record


def _incident_view(service: str = "payment-api") -> dict:
    return {
        "incident_id": f"INC-{uuid.uuid4().hex[:8]}",
        "service": service,
        "environment": "sandbox",
        "severity": "high",
        "status": "diagnosing",
        "kind": "recovery",
    }


def test_empty_when_nothing_recorded(sessions):
    deps = _Deps(sessions)
    assert knowledge_context(deps, _incident_view("nobody"), include_retrieval=False) == ""


def test_event_timeline_renders_cited_events(sessions):
    deps = _Deps(sessions)
    with sessions() as session:
        rec = _event_record(session, "payment-api", EventType.PR_OPENED)
    ctx = knowledge_context(deps, _incident_view(), include_retrieval=False)
    assert "Recent events" in ctx
    # Citation contract: the rendered line carries the event id the claim
    # can be traced to - not a bare assertion.
    assert f"event={rec.event_id}" in ctx
    assert "pr.opened" in ctx
    assert "alice" in ctx


def test_timeline_scoped_to_service(sessions):
    deps = _Deps(sessions)
    with sessions() as session:
        _event_record(session, "payment-api", EventType.PR_OPENED)
        _event_record(session, "other-svc", EventType.BUILD_FAILED)
    ctx = knowledge_context(deps, _incident_view("payment-api"), include_retrieval=False)
    assert "build.failed" not in ctx


def _seeded_store():
    from graph.memory_store import MemoryGraphStore
    from graph.schema import Edge, EdgeType, Node, NodeType

    store = MemoryGraphStore()
    store.upsert_node(Node(NodeType.DEVELOPER, "alice"))
    store.upsert_node(Node(NodeType.SERVICE, "payment-api"))
    store.upsert_node(Node(NodeType.SERVICE, "postgres"))
    store.upsert_node(Node(NodeType.INCIDENT, "INC-OLD"))
    store.upsert_edge(Edge(EdgeType.OWNS, "alice", "payment-api"))
    store.upsert_edge(Edge(EdgeType.DEPENDS_ON, "payment-api", "postgres"))
    store.upsert_edge(Edge(EdgeType.INVOLVES, "INC-OLD", "payment-api"))
    store.upsert_edge(Edge(EdgeType.RESOLVED, "bob", "INC-OLD"))
    store.upsert_node(Node(NodeType.DEVELOPER, "bob"))
    return store


def test_graph_context_when_enabled(sessions, monkeypatch):
    monkeypatch.setenv("HELIX_GRAPH_ENABLED", "true")
    store = _seeded_store()
    monkeypatch.setattr("graph.get_graph_store", lambda: store)
    deps = _Deps(sessions)
    ctx = knowledge_context(deps, _incident_view(), include_retrieval=False)
    assert "alice" in ctx  # ownership answer
    assert "postgres" in ctx  # dependency answer
    assert "INC-OLD" in ctx  # prior incident on the service
    assert "bob" in ctx  # who resolved it


def test_graph_context_off_without_flag(sessions, monkeypatch):
    monkeypatch.setenv("HELIX_GRAPH_ENABLED", "false")
    store = _seeded_store()
    monkeypatch.setattr("graph.get_graph_store", lambda: store)
    deps = _Deps(sessions)
    ctx = knowledge_context(deps, _incident_view(), include_retrieval=False)
    assert "alice" not in ctx  # graph off = pre-graph baseline


def test_graph_store_failure_collapses_to_empty(sessions, monkeypatch):
    monkeypatch.setenv("HELIX_GRAPH_ENABLED", "true")

    def _boom():
        raise RuntimeError("neo4j unreachable")

    monkeypatch.setattr("graph.get_graph_store", _boom)
    deps = _Deps(sessions)
    # Advisory aid: an unreachable store must never fail the run.
    assert knowledge_context(deps, _incident_view(), include_retrieval=False) == ""


def test_graph_neo4j_unreachable_mid_query(sessions, monkeypatch):
    """A store that raises inside a query (not at construction) also
    collapses gracefully - 2.7's advisory-fallback criterion."""
    monkeypatch.setenv("HELIX_GRAPH_ENABLED", "true")

    class _Broken:
        def neighbors(self, *a, **k):
            raise RuntimeError("connection lost")

        def get_node(self, *a, **k):
            return None

    monkeypatch.setattr("graph.get_graph_store", lambda: _Broken())
    deps = _Deps(sessions)
    assert knowledge_context(deps, _incident_view(), include_retrieval=False) == ""


def test_include_retrieval_composes_prior_incidents(sessions):
    """include_retrieval=True layers the E6 prior-incident block on top of
    the timeline - the composition order (retrieval, timeline, graph) is
    what the fused node's prompt relies on."""
    deps = _Deps(sessions)
    with sessions() as session:
        _event_record(session, "payment-api", EventType.PR_OPENED)
    ctx = knowledge_context(deps, _incident_view(), include_retrieval=True)
    # No prior resolved incidents exist, so the E6 block is empty - but
    # the timeline still renders.
    assert "Recent events" in ctx
