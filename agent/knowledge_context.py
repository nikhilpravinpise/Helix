"""The Knowledge Agent's context (subphase 2.8): what the organization
already knows about this system, rendered for the reasoning nodes.

GraphRAG here means composition, not a second retrieval stack: the E6
prior-incident retrieval stays the base layer, and this module adds the
two things it cannot see - the incident's own event timeline (what has
been happening on this service, from `events.history`) and the
relationship graph's neighborhood answers (who owns it, what it depends
on, which incidents touched it, who resolved similar ones, from
`graph.queries` over HELIX_GRAPH_ENABLED's store).

Every line carries a citation - `event_id`, incident id, or the
graph-edge description - so a claim the model repeats can be traced to a
record rather than taken on faith (2.8's exit criterion). And the same
contract `retrieval_context` established applies: knowledge context is
an advisory aid, so any failure while building it collapses to ""
rather than failing the run.
"""

from typing import TYPE_CHECKING, Any

from helix.config import get_settings

if TYPE_CHECKING:
    from agent.graph import GraphDeps


def _event_timeline(deps: "GraphDeps", service: str | None, limit: int = 10) -> str:
    """The newest recorded events touching this service, cited by event
    id - what the org's own history says has been happening here."""
    from events.history import search_history

    with deps.session_scope() as session:
        events = search_history(session, service=service, limit=limit)
    if not events:
        return ""
    lines = ["Recent events on this service (newest first):"]
    for event in events:
        detail = ""
        if event.metadata:
            interesting = {
                k: v
                for k, v in event.metadata.items()
                if k in ("reason", "stage", "pr", "branch", "status", "error")
            }
            if interesting:
                detail = " " + " ".join(f"{k}={v}" for k, v in interesting.items())
        lines.append(
            f"- [event={event.event_id}] {event.event_type.value} "
            f"by {event.actor} at {event.timestamp.isoformat()}{detail}"
        )
    return "\n".join(lines)


def _graph_context(service: str | None, incident_id: str | None) -> str:
    """The relationship-graph neighborhood for this service, in the
    question-and-cited-answer shape CONTEXT.md section 12 asks for.
    Empty when HELIX_GRAPH_ENABLED is off or the store is unreachable -
    the pre-graph baseline stays the fallback."""
    if not service or not get_settings().graph_enabled:
        return ""
    from graph import get_graph_store
    from graph.queries import (
        incidents_linked_to,
        owners_of,
        resolvers_of_similar_incidents,
        services_depended_on_by,
    )

    store = get_graph_store()
    try:
        sections: list[str] = []
        owners = owners_of(store, service)
        if owners:
            sections.append(
                "Service ownership (graph): " + ", ".join(owners) + f" - owns -> {service}"
            )
        dependencies = services_depended_on_by(store, service)
        if dependencies:
            sections.append(
                f"Services {service} depends on (graph): "
                + ", ".join(dependencies)
                + " - a failure here may originate there"
            )
        linked = [i for i in incidents_linked_to(store, service) if i != incident_id]
        if linked:
            sections.append(
                "Incidents that touched this service (graph): " + ", ".join(linked[:10])
            )
            resolvers = resolvers_of_similar_incidents(store, linked[:10])
            if resolvers:
                sections.append(
                    "People who resolved those incidents (graph): " + ", ".join(resolvers)
                )
        if not sections:
            return ""
        return "Relationship graph context:\n" + "\n".join(f"- {s}" for s in sections)
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


def knowledge_context(
    deps: "GraphDeps",
    incident: dict[str, Any],
    signature: dict[str, Any] | None = None,
    *,
    include_retrieval: bool = True,
) -> str:
    """The composed knowledge context, or "" when nothing applies.

    `include_retrieval` lets a caller that already renders the E6 block
    (diagnosis and planning pass `prior_context` separately) take only
    the event+graph portion, so nothing is duplicated in the prompt."""
    sections: list[str] = []
    if include_retrieval:
        from agent.retrieval_context import prior_incident_context

        base = prior_incident_context(deps, incident, signature)
        if base:
            sections.append(base)
    try:
        timeline = _event_timeline(deps, incident.get("service"))
        if timeline:
            sections.append(timeline)
    except Exception:
        pass  # advisory aid - never the reason a run fails
    try:
        graph = _graph_context(incident.get("service"), incident.get("incident_id"))
        if graph:
            sections.append(graph)
    except Exception:
        pass
    return "\n\n".join(sections)
