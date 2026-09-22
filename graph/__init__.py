"""The relationship graph package (subphase 2.7).

`get_graph_store()` is the same live/fixture factory shape every other
Helix capability uses: `HELIX_GRAPH_ENABLED=false` (the default) returns
an empty `MemoryGraphStore` per call - Triage/Review then get no
neighbors back from any query and fall through to their pre-graph
heuristics, which is the comparison baseline 2.7's exit criterion asks
for. `HELIX_GRAPH_ENABLED=true` returns a `Neo4jGraphStore` bound to
HELIX_NEO4J_*.
"""

from graph.store import GraphStore


def get_graph_store() -> GraphStore:
    """The live or memory graph store per HELIX_GRAPH_ENABLED."""
    from helix.config import get_settings

    settings = get_settings()
    if not settings.graph_enabled:
        from graph.memory_store import MemoryGraphStore

        return MemoryGraphStore()
    from graph.neo4j_store import Neo4jGraphStore

    return Neo4jGraphStore(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        database=settings.neo4j_database,
    )
