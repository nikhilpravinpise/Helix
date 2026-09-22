"""Triage Agent (subphase 2.5): what happened, what is affected, and who
owns it.

Runs once, at `RECEIVED` for a `change` incident (CONTEXT.md section 9:
"Uses GitHub, Jira, Notion, and Neo4j" - Jira/Notion projections do not
exist yet for a brand-new incident, so this pass uses GitHub's own event
metadata plus the service registry / graph). The coordinator turns its
report into two transitions - `RECEIVED -> TRIAGED` (impact identified)
then `TRIAGED -> OWNER_ASSIGNED` (ownership resolved) - or a single
`-> ESCALATED` when either the incident is a duplicate or ownership is
genuinely unknown, per 2.5's exit criterion: "escalate cleanly when
ownership is genuinely unknown", never guess.
"""

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from agent.actions.catalogue import SECRET_MANIFEST_PATHS
from agent.agents._change_context import change_context
from agent.ops.registry import load_registry, resolve_owner
from agent.state import WorkflowState
from helix.config import get_settings
from helix.observability import trace_workflow_step
from state import models
from state.machine import TERMINAL_STATUSES

if TYPE_CHECKING:
    from agent.graph import GraphDeps

def _blast_radius(files: list[str]) -> str:
    """A coarse, deterministic impact estimate from the changed paths -
    real signal (touching the Secret manifest is categorically riskier
    than touching a test file), not a guess."""
    if any(f in SECRET_MANIFEST_PATHS for f in files):
        return "high"
    if any("/k8s/" in f.replace("\\", "/") for f in files) or len(files) > 3:
        return "medium"
    return "low"


def _find_duplicate(
    session, incident: models.Incident, branch: str | None
) -> models.Incident | None:
    """Another OPEN `change` incident for the same service and branch -
    2.5's "duplicate incident detection against event history". Not
    correlation (E4 already merges same-PR redeliveries onto one incident
    via shared tokens): this catches a SECOND incident opened for what
    looks like the same underlying change - e.g. a re-opened PR on the
    same branch that E4's token set did not happen to share."""
    if not branch:
        return None
    candidates = session.scalars(
        select(models.Incident)
        .where(models.Incident.id != incident.id)
        .where(models.Incident.service == incident.service)
        .where(models.Incident.kind == models.IncidentKind.CHANGE)
        .where(models.Incident.status.not_in(TERMINAL_STATUSES))
        .order_by(models.Incident.created_at)
    ).all()
    for candidate in candidates:
        ctx = change_context(candidate)
        if ctx.get("branch") == branch:
            return candidate
    return None


def _graph_owner(service: str) -> tuple[str | None, str] | None:
    """The graph-backed owner answer when HELIX_GRAPH_ENABLED, else None
    (2.7: Triage switches to graph-backed context when the graph is on,
    with the pre-graph registry/CODEOWNERS answer kept as the comparison
    baseline - always computed, never discarded)."""
    if not get_settings().graph_enabled:
        return None
    from graph import get_graph_store
    from graph.queries import owners_of

    store = get_graph_store()
    try:
        owners = owners_of(store, service)
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    return (owners[0], "graph") if owners else None


def triage_node(deps: "GraphDeps"):
    """The triage graph node: impact + duplicate + ownership. Writes
    `triage`/`triage_complete` into state; the coordinator turns the
    report into TRIAGED/OWNER_ASSIGNED/ESCALATED transitions."""

    def _triage(state: WorkflowState) -> dict:
        with trace_workflow_step("triage"):
            with deps.session_scope() as session:
                incident = session.scalar(
                    select(models.Incident).where(
                        models.Incident.incident_id == state["incident_id"]
                    )
                )
                if incident is None:
                    return {
                        "errors": [
                            *state.get("errors", []),
                            f"triage: no such incident {state['incident_id']}",
                        ]
                    }
                attempt = state.get("attempts", 0)
                existing = session.scalar(
                    select(models.TriageResult)
                    .where(models.TriageResult.incident_id == incident.id)
                    .where(models.TriageResult.attempt == attempt)
                    .order_by(models.TriageResult.id.desc())
                    .limit(1)
                )
                if existing is not None:
                    # A crash between this node writing its row and the
                    # coordinator applying the transition resumed here -
                    # reuse the recorded finding rather than re-deriving
                    # (and re-inserting) it, the same idempotence
                    # `execution_idempotency_key` gives the Ops loop.
                    report = {
                        "affected_service": existing.affected_service,
                        "blast_radius": existing.blast_radius,
                        "owner": existing.owner,
                        "owner_source": existing.owner_source,
                        "duplicate_of": (
                            existing.duplicate_of.incident_id
                            if existing.duplicate_of is not None
                            else None
                        ),
                        "duplicate_of_pk": existing.duplicate_of_id,
                        "rationale": existing.rationale,
                    }
                    return {
                        "triage": report,
                        "triage_complete": True,
                        "errors": state.get("errors", []),
                    }
                ctx = change_context(incident)
                files = ctx.get("files") or []
                duplicate = _find_duplicate(session, incident, ctx.get("branch"))

                # "system" is the sentinel `change_context`/`EngineeringEvent`
                # use for "no real actor recorded" (events/schemas.py) -
                # never a legitimate commit-history ownership signal.
                # Treating it as one would let every incident from an
                # actor-less event silently "resolve" to an owner named
                # literally "system", defeating 2.5's "escalate when
                # ownership is genuinely unknown" exit criterion.
                author = ctx.get("author")
                commit_author = author if author and author != "system" else None
                registry_owner, registry_source = resolve_owner(
                    load_registry(),
                    incident.service,
                    changed_paths=files,
                    commit_author=commit_author,
                )
                graph_answer = _graph_owner(incident.service)
                owner, source = graph_answer if graph_answer else (registry_owner, registry_source)

                report: dict[str, Any] = {
                    "affected_service": incident.service,
                    "blast_radius": _blast_radius(files),
                    "owner": owner,
                    "owner_source": source,
                    "pre_graph_owner": registry_owner,
                    "pre_graph_owner_source": registry_source,
                    "duplicate_of": duplicate.incident_id if duplicate else None,
                    "duplicate_of_pk": duplicate.id if duplicate else None,
                    "rationale": (
                        f"duplicate of {duplicate.incident_id} (same service/branch)"
                        if duplicate
                        else (
                            f"owner {owner} resolved via {source}"
                            if owner
                            else "no owner resolved from codeowners, the service "
                            "registry, or commit history"
                        )
                    ),
                }
                session.add(
                    models.TriageResult(
                        incident_id=incident.id,
                        attempt=attempt,
                        affected_service=report["affected_service"],
                        blast_radius=report["blast_radius"],
                        duplicate_of_id=report["duplicate_of_pk"],
                        owner=owner,
                        owner_source=source,
                        rationale=report["rationale"],
                    )
                )
                return {
                    "triage": report,
                    "triage_complete": True,
                    "errors": state.get("errors", []),
                }

    return _triage
