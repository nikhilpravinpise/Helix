"""Graph ingestion from Postgres state and the service registry
(subphase 2.7)."""

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from agent.ops.registry import ServiceEntry, ServiceRegistry
from graph.ingest import ingest_incident, ingest_registry, sync_graph
from graph.memory_store import MemoryGraphStore
from graph.queries import incidents_linked_to, owners_of, reviewers_for
from graph.schema import NodeType
from state import models
from state.machine import IncidentStatus


def _incident(session: Session, **overrides) -> models.Incident:
    defaults = dict(
        incident_id="INC-graph001",
        fingerprint="f" * 64,
        service="payment-api",
        environment="sandbox",
        severity="medium",
        status=IncidentStatus.RECEIVED,
    )
    defaults.update(overrides)
    incident = models.Incident(**defaults)
    session.add(incident)
    session.flush()
    return incident


def test_ingest_registry_seeds_owners_and_reviewers() -> None:
    store = MemoryGraphStore()
    registry = ServiceRegistry(
        services={
            "payment-api": ServiceEntry.model_validate(
                {"owners": ["alice"], "default_reviewers": ["bob"]}
            )
        }
    )
    ingest_registry(store, registry)
    assert owners_of(store, "payment-api") == ["alice"]
    # Reviewers before owners: an owner is only the fallback reviewer.
    assert reviewers_for(store, "payment-api") == ["bob", "alice"]


def test_graph_answers_keep_the_registrys_order() -> None:
    """The default owner first, then the reviewers in the order the registry
    lists them - the same answers resolve_owner/resolve_reviewer give."""
    store = MemoryGraphStore()
    registry = ServiceRegistry(
        services={
            "payment-api": ServiceEntry.model_validate(
                {
                    "default_owner": "alice",
                    "owners": ["bob", "alice"],
                    "default_reviewers": ["bob", "carol"],
                }
            )
        }
    )
    ingest_registry(store, registry)
    assert owners_of(store, "payment-api") == ["alice", "bob"]
    assert reviewers_for(store, "payment-api") == ["bob", "carol", "alice"]
    assert reviewers_for(store, "payment-api", author="bob") == ["carol", "alice"]


def test_ingest_incident_links_service_and_owner(sqlite_engine: Engine) -> None:
    with Session(sqlite_engine) as session:
        incident = _incident(session, owner="alice", status=IncidentStatus.RECOVERED)
        store = MemoryGraphStore()
        ingest_incident(store, incident)
        assert incidents_linked_to(store, "payment-api") == ["INC-graph001"]
        node = store.get_node(NodeType.DEVELOPER, "alice")
        assert node is not None
        from graph.queries import resolvers_of_similar_incidents

        assert resolvers_of_similar_incidents(store, ["INC-graph001"]) == ["alice"]


def test_ingest_incident_open_incident_not_resolved_by_owner(sqlite_engine: Engine) -> None:
    """An owner assigned to a still-open incident has not RESOLVED it -
    only a RECOVERED incident counts."""
    with Session(sqlite_engine) as session:
        incident = _incident(session, owner="alice", status=IncidentStatus.DIAGNOSING)
        store = MemoryGraphStore()
        ingest_incident(store, incident)
        from graph.queries import resolvers_of_similar_incidents

        assert resolvers_of_similar_incidents(store, ["INC-graph001"]) == []


def test_ingest_incident_links_pr_and_deployment(sqlite_engine: Engine) -> None:
    with Session(sqlite_engine) as session:
        incident = _incident(session, linked_pr="42", linked_deployment="deploy-1")
        store = MemoryGraphStore()
        ingest_incident(store, incident, repo="org/repo")
        from graph.queries import pull_requests_that_caused

        assert pull_requests_that_caused(store, "deploy-1") == ["org/repo#42"]


def test_ingest_incident_links_work_item(sqlite_engine: Engine) -> None:
    with Session(sqlite_engine) as session:
        incident = _incident(session)
        session.add(
            models.WorkItem(
                incident_id=incident.id,
                provider="jira",
                external_id="1001",
                title="t",
                status="open",
            )
        )
        session.flush()
        session.refresh(incident)
        store = MemoryGraphStore()
        ingest_incident(store, incident)
        from graph.queries import work_item_for

        assert work_item_for(store, "INC-graph001") == "jira:1001"


def test_sync_graph_ingests_registry_and_every_incident(sqlite_engine: Engine) -> None:
    with Session(sqlite_engine) as session:
        _incident(session, incident_id="INC-a", fingerprint="a" * 64)
        _incident(session, incident_id="INC-b", fingerprint="b" * 64, service="other-service")
        store = MemoryGraphStore()
        count = sync_graph(session, store)
        assert count == 2
        assert set(incidents_linked_to(store, "payment-api")) == {"INC-a"}
        # The registry entry for payment-api is ingested even though no
        # incident touched its owners directly.
        assert set(owners_of(store, "payment-api")) == {"alice", "bob"}
