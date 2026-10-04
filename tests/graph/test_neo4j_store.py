"""Neo4jGraphStore against a fake driver (subphase 2.7).

No live Neo4j is required: `Neo4jGraphStore` accepts an injected `driver`
- the same seam `tests/agent/fakes.py` uses for the LLM client - so the
Cypher-shape of each method is exercised without a real bolt connection.
"""

from typing import Any

from graph.neo4j_store import Neo4jGraphStore
from graph.schema import Edge, EdgeType, Node, NodeType


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def single(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, driver: "_FakeDriver") -> None:
        self._driver = driver

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def run(self, query: str, **params: Any) -> _FakeResult:
        self._driver.queries.append((query, params))
        return self._driver.next_result


class _FakeDriver:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict]] = []
        self.next_result = _FakeResult([])

    def session(self, database: str) -> _FakeSession:
        return _FakeSession(self)

    def close(self) -> None:
        self.queries.append(("CLOSE", {}))


def _store() -> tuple[Neo4jGraphStore, _FakeDriver]:
    driver = _FakeDriver()
    store = Neo4jGraphStore("bolt://fake", "neo4j", "pw", driver=driver)
    return store, driver


def test_upsert_node_merges_via_cypher() -> None:
    store, driver = _store()
    store.upsert_node(Node(NodeType.SERVICE, "payment-api", properties={"team": "payments"}))
    query, params = driver.queries[-1]
    assert "MERGE (n:`service`" in query
    assert params == {"id": "payment-api", "props": {"team": "payments"}}


def test_upsert_edge_matches_both_endpoints() -> None:
    store, driver = _store()
    store.upsert_edge(Edge(EdgeType.OWNS, "alice", "payment-api"))
    query, params = driver.queries[-1]
    assert "MERGE (a)-[r:`OWNS`]->(b)" in query
    assert params == {"source": "alice", "target": "payment-api", "props": {}}


def test_get_node_returns_none_when_no_row() -> None:
    store, driver = _store()
    driver.next_result = _FakeResult([])
    assert store.get_node(NodeType.SERVICE, "unknown") is None


def test_get_node_builds_a_schema_node_from_the_row() -> None:
    store, driver = _store()
    driver.next_result = _FakeResult([{"n": {"id": "payment-api", "team": "payments"}}])
    node = store.get_node(NodeType.SERVICE, "payment-api")
    # Neo4j keeps the key as an `id` property; it is not node data, and
    # MemoryGraphStore returns the same node without it (checked against a
    # live Neo4j 5.26, where the two stores used to disagree).
    assert node == Node(NodeType.SERVICE, "payment-api", properties={"team": "payments"})


def test_neighbors_out_direction_query_shape() -> None:
    store, driver = _store()
    driver.next_result = _FakeResult([{"b": {"id": "payment-api"}, "labels": ["service"]}])
    result = store.neighbors("alice", edge_type=EdgeType.OWNS, direction="out")
    query, params = driver.queries[-1]
    assert "(a {id: $id})-[r:`OWNS`]->(b)" in query
    assert "ORDER BY coalesce(r.rank, 0)" in query
    assert params == {"id": "alice"}
    assert result == [Node(NodeType.SERVICE, "payment-api")]


def test_neighbors_in_direction_query_shape() -> None:
    store, driver = _store()
    driver.next_result = _FakeResult([])
    store.neighbors("payment-api", edge_type=EdgeType.OWNS, direction="in")
    query, _ = driver.queries[-1]
    assert "(a {id: $id})<-[r:`OWNS`]-(b)" in query


def test_neighbors_invalid_direction_raises() -> None:
    store, _ = _store()
    import pytest

    with pytest.raises(ValueError):
        store.neighbors("x", direction="sideways")


def test_clear_deletes_only_helix_nodes() -> None:
    """clear() must not wipe a shared database - only nodes carrying
    Helix's own labels go."""
    store, driver = _store()
    store.clear()
    query, params = driver.queries[-1]
    assert "DETACH DELETE" in query
    assert "labels(n) IN $labels" in query or "l IN $labels" in query
    assert set(params["labels"]) == {t.value for t in NodeType}


def test_ensure_constraints_runs_one_per_node_type() -> None:
    store, driver = _store()
    store.ensure_constraints()
    assert len(driver.queries) == len(NodeType)
    assert all("CREATE CONSTRAINT IF NOT EXISTS" in q for q, _ in driver.queries)


def test_close_closes_the_driver() -> None:
    store, driver = _store()
    store.close()
    assert ("CLOSE", {}) in driver.queries


def test_a_node_reads_back_identically_from_both_stores() -> None:
    """GraphStore's contract: the same write reads back as the same Node,
    whichever store holds it. The Neo4j store used to add its storage key
    to `properties`, so the fake and the real store disagreed."""
    from graph.memory_store import MemoryGraphStore

    written = Node(NodeType.SERVICE, "payment-api", properties={"team": "payments"})
    memory = MemoryGraphStore()
    memory.upsert_node(written)
    store, driver = _store()
    driver.next_result = _FakeResult([{"n": {"id": "payment-api", "team": "payments"}}])
    assert store.get_node(NodeType.SERVICE, "payment-api") == memory.get_node(
        NodeType.SERVICE, "payment-api"
    )
