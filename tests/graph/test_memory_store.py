"""MemoryGraphStore + graph.queries over CONTEXT.md section 12's example
graph (subphase 2.7)."""

import pytest

from graph.memory_store import MemoryGraphStore
from graph.queries import (
    incidents_linked_to,
    owners_of,
    pull_requests_that_caused,
    resolvers_of_similar_incidents,
    reviewers_for,
    services_depended_on_by,
)
from graph.schema import Edge, EdgeType, Node, NodeType


@pytest.fixture
def store() -> MemoryGraphStore:
    s = MemoryGraphStore()
    s.upsert_node(Node(NodeType.DEVELOPER, "alice"))
    s.upsert_node(Node(NodeType.DEVELOPER, "bob"))
    s.upsert_node(Node(NodeType.SERVICE, "payment-api"))
    s.upsert_node(Node(NodeType.DATABASE, "payments-db"))
    s.upsert_node(Node(NodeType.PULL_REQUEST, "org/repo#42"))
    s.upsert_node(Node(NodeType.DEPLOYMENT, "deploy-72"))
    s.upsert_node(Node(NodeType.INCIDENT, "INC-1"))
    s.upsert_node(Node(NodeType.INCIDENT, "INC-2"))
    s.upsert_edge(Edge(EdgeType.OWNS, "alice", "payment-api"))
    s.upsert_edge(Edge(EdgeType.REVIEWS, "bob", "payment-api"))
    s.upsert_edge(Edge(EdgeType.DEPENDS_ON, "payment-api", "payments-db"))
    s.upsert_edge(Edge(EdgeType.MODIFIES, "org/repo#42", "payment-api"))
    s.upsert_edge(Edge(EdgeType.CAUSED, "org/repo#42", "deploy-72"))
    s.upsert_edge(Edge(EdgeType.INVOLVES, "INC-1", "payment-api"))
    s.upsert_edge(Edge(EdgeType.INVOLVES, "INC-2", "payment-api"))
    s.upsert_edge(Edge(EdgeType.RESOLVED, "alice", "INC-1"))
    return s


def test_who_owns_payment_service(store: MemoryGraphStore) -> None:
    assert owners_of(store, "payment-api") == ["alice"]


def test_who_owns_unknown_service_is_empty(store: MemoryGraphStore) -> None:
    assert owners_of(store, "unknown-service") == []


def test_reviewer_excludes_the_author_who_is_also_owner(store: MemoryGraphStore) -> None:
    # alice owns payment-api; if alice authored the change, she is
    # excluded and bob (a reviewer) remains.
    assert reviewers_for(store, "payment-api", author="alice") == ["bob"]


def test_reviewer_with_no_author_returns_every_candidate(store: MemoryGraphStore) -> None:
    candidates = reviewers_for(store, "payment-api")
    assert set(candidates) == {"alice", "bob"}


def test_who_fixed_a_similar_incident(store: MemoryGraphStore) -> None:
    assert resolvers_of_similar_incidents(store, ["INC-1", "INC-2"]) == ["alice"]


def test_which_pr_caused_this_deployment(store: MemoryGraphStore) -> None:
    assert pull_requests_that_caused(store, "deploy-72") == ["org/repo#42"]


def test_which_incidents_are_linked_to_this_service(store: MemoryGraphStore) -> None:
    assert set(incidents_linked_to(store, "payment-api")) == {"INC-1", "INC-2"}


def test_services_depended_on_by(store: MemoryGraphStore) -> None:
    assert services_depended_on_by(store, "payment-api") == ["payments-db"]


def test_upsert_node_merges_properties_rather_than_replacing() -> None:
    s = MemoryGraphStore()
    s.upsert_node(Node(NodeType.SERVICE, "payment-api", properties={"team": "payments"}))
    s.upsert_node(Node(NodeType.SERVICE, "payment-api", properties={"tier": "critical"}))
    node = s.get_node(NodeType.SERVICE, "payment-api")
    assert node is not None
    assert node.properties == {"team": "payments", "tier": "critical"}


def test_upsert_edge_is_idempotent() -> None:
    s = MemoryGraphStore()
    s.upsert_node(Node(NodeType.DEVELOPER, "alice"))
    s.upsert_node(Node(NodeType.SERVICE, "payment-api"))
    s.upsert_edge(Edge(EdgeType.OWNS, "alice", "payment-api"))
    s.upsert_edge(Edge(EdgeType.OWNS, "alice", "payment-api"))
    assert owners_of(s, "payment-api") == ["alice"]


def test_neighbors_invalid_direction_raises() -> None:
    s = MemoryGraphStore()
    with pytest.raises(ValueError):
        s.neighbors("x", direction="sideways")


def test_clear_empties_the_store(store: MemoryGraphStore) -> None:
    store.clear()
    assert owners_of(store, "payment-api") == []
    assert store.nodes_by_type(NodeType.SERVICE) == []
