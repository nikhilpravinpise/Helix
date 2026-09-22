"""Live `GraphStore` over Neo4j (subphase 2.7).

The `neo4j` driver is imported lazily inside `__init__`, exactly how
`agent.graph.open_checkpointer` defers its `langgraph.checkpoint.postgres`
import - so importing this module never requires a running Neo4j instance
or even the driver being importable unless a caller actually asks for the
live store. Every method maps to plain Cypher over `Node.type` as a
label and `id` as the node key; `Record`/`Node`/`Relationship` driver
objects never escape this module (C8) - each method returns
`graph.schema.Node` instances built from plain dict field access.
"""

from typing import Any

from graph.schema import Edge, EdgeType, Node, NodeType


class Neo4jGraphStore:
    """Cypher-backed `GraphStore`. One node label per `NodeType`, node
    identity is the `id` property (uniqueness enforced by a constraint
    `ensure_constraints()` creates), and every edge is a relationship of
    type `EdgeType.value` carrying `properties` as relationship
    properties."""

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        *,
        database: str = "neo4j",
        driver: Any = None,
    ) -> None:
        if driver is not None:
            self._driver = driver
        else:
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database

    def close(self) -> None:
        self._driver.close()

    def ensure_constraints(self) -> None:
        """One uniqueness constraint per node label - idempotent (`IF NOT
        EXISTS`), safe to run on every startup like `PostgresSaver.setup()`."""
        with self._driver.session(database=self._database) as session:
            for node_type in NodeType:
                session.run(
                    f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:`{node_type.value}`) "
                    "REQUIRE n.id IS UNIQUE"
                )

    def upsert_node(self, node: Node) -> None:
        with self._driver.session(database=self._database) as session:
            session.run(
                f"MERGE (n:`{node.type.value}` {{id: $id}}) SET n += $props",
                id=node.id,
                props=dict(node.properties),
            )

    def upsert_edge(self, edge: Edge) -> None:
        with self._driver.session(database=self._database) as session:
            session.run(
                "MATCH (a {id: $source}), (b {id: $target}) "
                f"MERGE (a)-[r:`{edge.type.value}`]->(b) SET r += $props",
                source=edge.source,
                target=edge.target,
                props=dict(edge.properties),
            )

    def get_node(self, node_type: NodeType, node_id: str) -> Node | None:
        with self._driver.session(database=self._database) as session:
            record = session.run(
                f"MATCH (n:`{node_type.value}` {{id: $id}}) RETURN n", id=node_id
            ).single()
            if record is None:
                return None
            return Node(type=node_type, id=node_id, properties=dict(record["n"]))

    def nodes_by_type(self, node_type: NodeType) -> list[Node]:
        with self._driver.session(database=self._database) as session:
            result = session.run(f"MATCH (n:`{node_type.value}`) RETURN n")
            return [Node(type=node_type, id=r["n"]["id"], properties=dict(r["n"])) for r in result]

    def neighbors(
        self,
        node_id: str,
        *,
        edge_type: EdgeType | None = None,
        direction: str = "out",
        target_type: NodeType | None = None,
    ) -> list[Node]:
        if direction not in ("out", "in"):
            raise ValueError(f"direction must be 'out' or 'in', got {direction!r}")
        rel = f":`{edge_type.value}`" if edge_type is not None else ""
        label = f":`{target_type.value}`" if target_type is not None else ""
        pattern = (
            f"(a {{id: $id}})-[{rel}]->(b{label})"
            if direction == "out"
            else f"(a {{id: $id}})<-[{rel}]-(b{label})"
        )
        with self._driver.session(database=self._database) as session:
            result = session.run(f"MATCH {pattern} RETURN b, labels(b) AS labels", id=node_id)
            out: list[Node] = []
            for record in result:
                labels = record["labels"]
                node_type_found = NodeType(labels[0]) if labels else None
                if node_type_found is None:
                    continue
                out.append(
                    Node(type=node_type_found, id=record["b"]["id"], properties=dict(record["b"]))
                )
            return out

    def clear(self) -> None:
        # Scope the wipe to Helix's own vocabulary: the configured
        # database may be shared, and `MATCH (n) DETACH DELETE n` would
        # delete subgraphs that are not ours to delete. Nodes carrying
        # none of our NodeType labels are left untouched.
        labels = [t.value for t in NodeType]
        with self._driver.session(database=self._database) as session:
            session.run(
                "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $labels) DETACH DELETE n",
                labels=labels,
            )
