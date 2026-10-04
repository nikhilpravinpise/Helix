"""Graph ingestion (subphase 2.7): PostgreSQL state, the service registry,
and GitHub PR/deployment links become graph nodes and edges.

Deliberately NOT wired into `state.machine.transition()`. Every state
change already commits through Postgres first (the outbox principle this
codebase applies everywhere - E5's events, 2.2-2.4's integration_outbox);
making a Neo4j write part of that same transaction would make the graph
database a hard dependency of the recovery loop, exactly the coupling
CONTEXT.md section 22 warns against ("no integration added for the
architecture diagram alone"). Instead `sync_graph()` re-derives the
graph from the current Postgres rows and the registry - upserts are
idempotent, so running it after every batch of new incidents/events is
"incremental update from the event stream" in effect: the graph converges
to current truth without ever being in the write path of a recovery
decision. `helix graph-sync` (helix/cli.py) runs it on demand; the
benchmark harness and tests call `sync_graph` directly.
"""

from typing import Any

from graph.schema import Edge, EdgeType, Node, NodeType
from graph.store import GraphStore
from state import models
from state.machine import IncidentStatus


def ingest_registry(store: GraphStore, registry) -> None:
    """Seed Developer/Service/OWNS/REVIEWS from the service registry - the
    "code ownership... and the service registry" ingestion source 2.7
    names alongside GitHub and Postgres state.

    REVIEWS is recorded directly against the Service node rather than a
    Repository node: CONTEXT.md section 12's example graph draws
    `Developer --REVIEWS--> Repository`, but this sandbox maps one
    service to one repository 1:1, so a Service -> Repository hop would
    be pure indirection for `graph.queries.reviewers_for`. A
    multi-repository service is a concrete, repeated need this schema
    does not have yet (CLAUDE.md: add an abstraction only when one does).
    """
    for service, entry in registry.services.items():
        store.upsert_node(Node(type=NodeType.SERVICE, id=service, properties={"team": entry.team}))
        if entry.repo:
            store.upsert_node(Node(type=NodeType.REPOSITORY, id=entry.repo))
        # `rank` keeps the registry's order (default owner first, reviewers
        # as listed): neighbors() returns edges by rank, and Neo4j promises
        # no order of its own.
        owners = list(dict.fromkeys([*filter(None, [entry.default_owner]), *entry.owners]))
        for rank, owner in enumerate(owners):
            store.upsert_node(Node(type=NodeType.DEVELOPER, id=owner))
            store.upsert_edge(
                Edge(type=EdgeType.OWNS, source=owner, target=service, properties={"rank": rank})
            )
        for rank, reviewer in enumerate(entry.default_reviewers):
            store.upsert_node(Node(type=NodeType.DEVELOPER, id=reviewer))
            store.upsert_edge(
                Edge(
                    type=EdgeType.REVIEWS,
                    source=reviewer,
                    target=service,
                    properties={"rank": rank},
                )
            )
        for dep in entry.depends_on:
            # The dependency may be a database or queue rather than a
            # registered service - a bare node keyed by name keeps the
            # edge honest either way.
            store.upsert_node(Node(type=NodeType.SERVICE, id=dep))
            store.upsert_edge(Edge(type=EdgeType.DEPENDS_ON, source=service, target=dep))


def _pr_node_id(repo: str, pr_ref: str) -> str:
    """A PR's graph id: `owner/repo#123` when `pr_ref` is a bare number,
    or `pr_ref` unchanged when it already carries repo context."""
    return pr_ref if "#" in pr_ref else f"{repo}#{pr_ref}"


def ingest_incident(store: GraphStore, incident: models.Incident, *, repo: str = "") -> None:
    """One incident's graph facts: itself, the service it involves, its
    owner (if resolved), its linked PR/deployment, its work item and
    knowledge page (if projected), and - once recovered - who resolved
    it, which is exactly the substrate `resolvers_of_similar_incidents`
    (2.8's GraphRAG) needs."""
    store.upsert_node(Node(type=NodeType.SERVICE, id=incident.service))
    store.upsert_node(
        Node(
            type=NodeType.INCIDENT,
            id=incident.incident_id,
            properties={
                "service": incident.service,
                "environment": incident.environment,
                "severity": incident.severity,
                "status": incident.status.value,
                "kind": incident.kind.value,
            },
        )
    )
    store.upsert_edge(
        Edge(type=EdgeType.INVOLVES, source=incident.incident_id, target=incident.service)
    )

    if incident.owner:
        store.upsert_node(Node(type=NodeType.DEVELOPER, id=incident.owner))
        if incident.status == IncidentStatus.RECOVERED:
            store.upsert_edge(
                Edge(type=EdgeType.RESOLVED, source=incident.owner, target=incident.incident_id)
            )

    if incident.linked_pr:
        pr_id = _pr_node_id(repo, incident.linked_pr)
        store.upsert_node(Node(type=NodeType.PULL_REQUEST, id=pr_id))
        store.upsert_edge(Edge(type=EdgeType.MODIFIES, source=pr_id, target=incident.service))
        if incident.linked_deployment:
            store.upsert_node(Node(type=NodeType.DEPLOYMENT, id=incident.linked_deployment))
            store.upsert_edge(
                Edge(type=EdgeType.CAUSED, source=pr_id, target=incident.linked_deployment)
            )

    for work_item in incident.work_items:
        wi_id = f"{work_item.provider}:{work_item.external_id}"
        store.upsert_node(
            Node(
                type=NodeType.WORK_ITEM,
                id=wi_id,
                properties={"provider": work_item.provider, "status": work_item.status},
            )
        )
        store.upsert_edge(Edge(type=EdgeType.TRACKED_BY, source=incident.incident_id, target=wi_id))

    if incident.knowledge_page is not None:
        page = incident.knowledge_page
        page_id = f"{page.provider}:{page.external_id}"
        store.upsert_node(Node(type=NodeType.KNOWLEDGE_PAGE, id=page_id))
        store.upsert_edge(
            Edge(type=EdgeType.DOCUMENTED_IN, source=incident.incident_id, target=page_id)
        )


def sync_graph(session: Any, store: GraphStore, *, repo: str = "") -> int:
    """Re-derive the graph from every incident currently in Postgres plus
    the service registry. Idempotent - safe to call after every new batch
    of incidents/events (`helix graph-sync`). Returns the incident count
    ingested.

    The registry is loaded and the incident rows are read first, then
    the store is cleared and the rebuild is written: a projection that
    only upserts keeps facts Postgres already deleted (a work-item
    mirror reconcile removed, a page replaced) as phantom edges forever,
    but a failure before the clear leaves the current projection intact.
    Derive means replace - the rebuild below carries every current fact,
    so a cleared store converges to exactly today's truth."""
    from sqlalchemy import select

    from agent.ops.registry import load_registry

    registry = load_registry()
    incidents = session.scalars(select(models.Incident)).all()
    store.clear()
    ingest_registry(store, registry)
    for incident in incidents:
        ingest_incident(store, incident, repo=repo)
    return len(incidents)
