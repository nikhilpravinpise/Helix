"""The graph store capability (subphase 2.7).

One protocol, two implementations - the exact live/fixture split every
other Helix capability already uses (`integrations.capabilities`):
`MemoryGraphStore` (`graph/memory_store.py`) for unit tests and any run
with `HELIX_GRAPH_ENABLED=false`, and `Neo4jGraphStore`
(`graph/neo4j_store.py`) over a real Neo4j instance. Callers depend on
`GraphStore` only - never a `neo4j.Record` or a raw Cypher result crosses
into agent code (C8 applied to the graph store the way it already applies
to every evidence capability).
"""

from typing import Protocol

from graph.schema import Edge, EdgeType, Node, NodeType


class GraphStore(Protocol):
    """Read/write access to the relationship graph."""

    def upsert_node(self, node: Node) -> None:
        """Insert or update one node by (type, id). Properties merge over
        any existing record rather than replacing it wholesale, so
        incremental ingestion (2.7's "incremental update from the event
        stream") never has to re-supply everything it already knew."""
        ...

    def upsert_edge(self, edge: Edge) -> None:
        """Insert or update one directed edge. Idempotent on
        (type, source, target): re-ingesting the same relationship never
        creates a duplicate."""
        ...

    def get_node(self, node_type: NodeType, node_id: str) -> Node | None:
        """The node at (type, id), or None."""
        ...

    def nodes_by_type(self, node_type: NodeType) -> list[Node]:
        """Every node of one type, insertion order."""
        ...

    def neighbors(
        self,
        node_id: str,
        *,
        edge_type: EdgeType | None = None,
        direction: str = "out",
        target_type: NodeType | None = None,
    ) -> list[Node]:
        """Nodes reachable from `node_id` by one hop. `direction` is
        `"out"` (edges where `node_id` is the source) or `"in"` (edges
        where it is the target); `edge_type`/`target_type` narrow the
        walk. Unknown `node_id` returns an empty list, never an error -
        "the graph knows nothing about this yet" is a normal, honest
        answer for a freshly-ingested or partially-populated graph.
        """
        ...

    def clear(self) -> None:
        """Drop everything - full re-ingestion (2.7) starts here."""
        ...
