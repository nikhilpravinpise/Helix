"""In-memory `GraphStore` (subphase 2.7): the default and the test double.

Used whenever `HELIX_GRAPH_ENABLED` is false (Triage/Review then run their
pre-graph heuristics - 2.7's own comparison baseline) and in every unit
test, the same role `integrations.fixtures.FixtureKubernetesEvidence`
plays for the evidence layer. Node ids are namespaced by type internally
so a service and an incident may coincidentally share a string id without
colliding.
"""

from collections import defaultdict

from graph.schema import Edge, EdgeType, Node, NodeType


def _key(node_type: NodeType, node_id: str) -> tuple[NodeType, str]:
    return (node_type, node_id)


class MemoryGraphStore:
    """Dict-backed graph. Not thread-safe, matching the other fixture
    implementations' scope (one process, one run)."""

    def __init__(self) -> None:
        self._nodes: dict[tuple[NodeType, str], Node] = {}
        self._out: dict[str, list[Edge]] = defaultdict(list)
        self._in: dict[str, list[Edge]] = defaultdict(list)

    def upsert_node(self, node: Node) -> None:
        key = _key(node.type, node.id)
        existing = self._nodes.get(key)
        if existing is None:
            self._nodes[key] = node
            return
        merged = {**existing.properties, **node.properties}
        self._nodes[key] = Node(type=node.type, id=node.id, properties=merged)

    def upsert_edge(self, edge: Edge) -> None:
        existing_out = self._out[edge.source]
        for i, e in enumerate(existing_out):
            if e.type == edge.type and e.target == edge.target:
                merged = Edge(
                    type=edge.type,
                    source=edge.source,
                    target=edge.target,
                    properties={**e.properties, **edge.properties},
                )
                existing_out[i] = merged
                in_list = self._in[edge.target]
                for j, e2 in enumerate(in_list):
                    if e2.type == edge.type and e2.source == edge.source:
                        in_list[j] = merged
                return
        self._out[edge.source].append(edge)
        self._in[edge.target].append(edge)

    def get_node(self, node_type: NodeType, node_id: str) -> Node | None:
        return self._nodes.get(_key(node_type, node_id))

    def nodes_by_type(self, node_type: NodeType) -> list[Node]:
        return [n for (t, _), n in self._nodes.items() if t == node_type]

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
        edges = self._out.get(node_id, []) if direction == "out" else self._in.get(node_id, [])
        out: list[Node] = []
        for edge in edges:
            if edge_type is not None and edge.type != edge_type:
                continue
            other_id = edge.target if direction == "out" else edge.source
            for (t, i), node in self._nodes.items():
                if i != other_id:
                    continue
                if target_type is not None and t != target_type:
                    continue
                out.append(node)
        return out

    def clear(self) -> None:
        self._nodes.clear()
        self._out.clear()
        self._in.clear()
