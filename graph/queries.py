"""The CONTEXT.md section 12 example questions, answered against a
`GraphStore` (subphase 2.7).

Each function is a thin, pure wrapper over `GraphStore.neighbors` - the
graph carries the relationships CONTEXT.md section 12 declares
(`OWNS`, `REVIEWS`, `DEPENDS_ON`, `MODIFIES`, `CAUSED`, `INVOLVES`,
`RESOLVED`, `TRACKED_BY`, `DOCUMENTED_IN`), so answering "who owns
PaymentService" is one directed hop, not a bespoke query per question.
Every function returns node ids (developer handles, incident ids, PR
references) rather than `Node` objects, so a caller never has to unwrap
one - the same "return the fact, not the record" shape `resolve_owner`
already uses.
"""

from graph.schema import EdgeType, NodeType
from graph.store import GraphStore


def owners_of(store: GraphStore, service: str) -> list[str]:
    """Who owns `service`? Developer --OWNS--> Service."""
    return [
        n.id
        for n in store.neighbors(
            service, edge_type=EdgeType.OWNS, direction="in", target_type=NodeType.DEVELOPER
        )
    ]


def reviewers_for(store: GraphStore, service: str, *, author: str | None = None) -> list[str]:
    """Who should review a change to `service`? Its reviewers in registry
    order, then its owners as the fallback, excluding the change's own
    author - "who should review this PR if the owner authored it"
    (CONTEXT.md section 12) resolves by construction: the author is
    filtered out and the rest are what is left to choose from.

    Reviewers come first because that is who the registry names to
    review: listing owners first made the owner review every change, and
    the graph-on benchmark scored reviewer accuracy 0/4 against 4/4."""
    candidates = list(
        dict.fromkeys(
            [
                n.id
                for n in store.neighbors(
                    service,
                    edge_type=EdgeType.REVIEWS,
                    direction="in",
                    target_type=NodeType.DEVELOPER,
                )
            ]
            + [
                n.id
                for n in store.neighbors(
                    service,
                    edge_type=EdgeType.OWNS,
                    direction="in",
                    target_type=NodeType.DEVELOPER,
                )
            ]
        )
    )
    if author:
        candidates = [c for c in candidates if c != author]
    return candidates


def resolvers_of_similar_incidents(store: GraphStore, incident_ids: list[str]) -> list[str]:
    """Who fixed a similar incident? Given a set of prior incident ids
    (typically from E6/2.8's lexical or knowledge retrieval),
    Developer --RESOLVED--> Incident for each, deduplicated in
    first-seen order so the most-recently-matched incident's resolver is
    favored."""
    out: list[str] = []
    seen: set[str] = set()
    for incident_id in incident_ids:
        for node in store.neighbors(
            incident_id,
            edge_type=EdgeType.RESOLVED,
            direction="in",
            target_type=NodeType.DEVELOPER,
        ):
            if node.id not in seen:
                seen.add(node.id)
                out.append(node.id)
    return out


def pull_requests_that_caused(store: GraphStore, deployment_id: str) -> list[str]:
    """Which PR caused this deployment? PR --CAUSED--> Deployment."""
    return [
        n.id
        for n in store.neighbors(
            deployment_id,
            edge_type=EdgeType.CAUSED,
            direction="in",
            target_type=NodeType.PULL_REQUEST,
        )
    ]


def incidents_linked_to(store: GraphStore, service: str) -> list[str]:
    """Which incidents are linked to this service? Incident --INVOLVES--> Service."""
    return [
        n.id
        for n in store.neighbors(
            service, edge_type=EdgeType.INVOLVES, direction="in", target_type=NodeType.INCIDENT
        )
    ]


def services_depended_on_by(store: GraphStore, service: str) -> list[str]:
    """What does `service` depend on? Service --DEPENDS_ON--> Database (or
    another Service) - the blast-radius primitive Triage's impact
    analysis (2.5) uses once the graph is enabled."""
    return [n.id for n in store.neighbors(service, edge_type=EdgeType.DEPENDS_ON, direction="out")]


def work_item_for(store: GraphStore, incident_id: str) -> str | None:
    """The tracked work item for an incident, if the graph has ingested
    one. Incident --TRACKED_BY--> WorkItem."""
    nodes = store.neighbors(
        incident_id,
        edge_type=EdgeType.TRACKED_BY,
        direction="out",
        target_type=NodeType.WORK_ITEM,
    )
    return nodes[0].id if nodes else None


def knowledge_page_for(store: GraphStore, incident_id: str) -> str | None:
    """The knowledge page documenting an incident, if any.
    Incident --DOCUMENTED_IN--> KnowledgePage."""
    nodes = store.neighbors(
        incident_id,
        edge_type=EdgeType.DOCUMENTED_IN,
        direction="out",
        target_type=NodeType.KNOWLEDGE_PAGE,
    )
    return nodes[0].id if nodes else None
