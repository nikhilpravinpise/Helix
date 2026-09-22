"""Review Agent (subphase 2.6): who should review this change, and why.

Runs at `OWNER_ASSIGNED` (first request) and again on re-entry to
`REVIEW_PENDING` from `FIX_REQUIRED` (CONTEXT.md section 14's
`REJECTED -> FIX_REQUIRED -> REVIEW_PENDING` loop) once the coordinator
sees an updated commit. `resolve_reviewer` structurally excludes the
change's own author - "self-review is never proposed" is enforced there,
not left to this node to remember - and the request is delivered through
Slack (2.2) as an outbox entry once a `review_requests` row exists, never
before: the durable row is the decision, Slack is the delivery surface.
"""

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from agent.agents._change_context import change_context
from agent.ops.registry import load_registry, resolve_reviewer
from agent.state import WorkflowState
from helix.config import get_settings
from helix.observability import trace_workflow_step
from state import models

if TYPE_CHECKING:
    from agent.graph import GraphDeps


def _declined_reviewers(session, incident_pk: int) -> set[str]:
    """Reviewers who already rejected this change - never proposed again
    on a re-request, the same "never re-select a failed candidate"
    principle the Ops loop's exclusion list already applies."""
    return set(
        session.scalars(
            select(models.ReviewRequest.reviewer)
            .where(models.ReviewRequest.incident_id == incident_pk)
            .where(models.ReviewRequest.status == models.ApprovalStatus.REJECTED)
        )
    )


def _graph_reviewer(
    service: str, author: str | None, exclude: set[str]
) -> tuple[str | None, str] | None:
    """The graph-backed reviewer answer when HELIX_GRAPH_ENABLED (2.7),
    else None - Review keeps the pre-graph registry answer as the
    comparison baseline regardless."""
    if not get_settings().graph_enabled:
        return None
    from graph import get_graph_store
    from graph.queries import reviewers_for

    candidates = [
        c for c in reviewers_for(get_graph_store(), service, author=author) if c not in exclude
    ]
    return (candidates[0], "graph") if candidates else None


def review_node(deps: "GraphDeps"):
    """The review graph node: resolve a reviewer, persist the pending
    request. Writes `review`/`review_complete` into state; the
    coordinator turns the report into REVIEW_PENDING/ESCALATED."""

    def _review(state: WorkflowState) -> dict:
        with trace_workflow_step("review"):
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
                            f"review: no such incident {state['incident_id']}",
                        ]
                    }
                pending = session.scalar(
                    select(models.ReviewRequest)
                    .where(models.ReviewRequest.incident_id == incident.id)
                    .where(models.ReviewRequest.status == models.ApprovalStatus.PENDING)
                    .order_by(models.ReviewRequest.id.desc())
                    .limit(1)
                )
                if pending is not None:
                    # A pending request already exists (a crash between
                    # this node writing it and the coordinator applying
                    # the transition resumed here) - report it rather
                    # than opening a second one, which
                    # ux_review_requests_one_pending would refuse anyway.
                    report = {
                        "reviewer": pending.reviewer,
                        "rationale": pending.rationale,
                        "pre_graph_reviewer": pending.reviewer,
                        "pre_graph_rationale": pending.rationale,
                    }
                    return {
                        "review": report,
                        "review_complete": True,
                        "errors": state.get("errors", []),
                    }
                ctx = change_context(incident)
                declined = _declined_reviewers(session, incident.id)
                registry_reviewer, registry_rationale = resolve_reviewer(
                    load_registry(), incident.service, author=ctx.get("author"), exclude=declined
                )
                graph_answer = _graph_reviewer(incident.service, ctx.get("author"), declined)
                reviewer, rationale = (
                    (graph_answer[0], f"graph-backed: {graph_answer[1]}")
                    if graph_answer
                    else (registry_reviewer, registry_rationale)
                )

                report: dict[str, Any] = {
                    "reviewer": reviewer,
                    "rationale": rationale,
                    "pre_graph_reviewer": registry_reviewer,
                    "pre_graph_rationale": registry_rationale,
                    "author": ctx.get("author"),
                }
                if reviewer is not None:
                    review_row = models.ReviewRequest(
                        incident_id=incident.id,
                        attempt=state.get("attempts", 0),
                        reviewer=reviewer,
                        rationale=rationale,
                        status=models.ApprovalStatus.PENDING,
                    )
                    session.add(review_row)
                    session.flush()
                    # 2.6: the request reaches the reviewer through Slack
                    # with the reasoning attached - as an outbox row in
                    # this same transaction, so the durable request never
                    # depends on Slack being up.
                    from integrations.lifecycle_sync import enqueue_review_request

                    enqueue_review_request(session, incident, review_row)
                return {
                    "review": report,
                    "review_complete": True,
                    "errors": state.get("errors", []),
                }

    return _review
