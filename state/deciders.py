"""Who may decide an approval or a review (Phase 2 human-in-the-loop rules).

Every surface - the CLI, the API, Slack, the benchmark's scripted
deciders - records a decision through `state.approvals.record_decision` or
`state.reviews.record_decision`, and both ask this module first:

- an approval is decided only by an owner of the incident's service, as
  the service registry names them;
- a review is decided only by the reviewer it was requested from;
- the author of the change decides neither.

Identities are registry handles (GitHub logins). The CLI's identity is
HELIX_OPERATOR or `--actor`, the API's is the request body's `actor`, and
Slack's is the handle whose `people.<handle>.slack` matches the clicking
user. The CLI and API identities are self-asserted: they are only as
trustworthy as access to the CLI and to the intake token.
"""

from state import models


def _author(incident: models.Incident) -> str | None:
    """The change's author, or None for a recovery incident or an
    actor-less event (`system` is the schema's "nobody" sentinel)."""
    from agent.agents._change_context import change_context

    author = change_context(incident).get("author")
    return None if author in (None, "", "system") else str(author)


def approval_deciders(incident: models.Incident) -> list[str]:
    """The handles allowed to decide an approval on `incident`, default
    owner first."""
    from agent.ops.registry import load_registry

    entry = load_registry().get(incident.service)
    if entry is None:
        return []
    owners = [*filter(None, [entry.default_owner]), *entry.owners]
    author = _author(incident)
    return [owner for owner in dict.fromkeys(owners) if owner != author]


def approval_refusal(incident: models.Incident, actor: str) -> str | None:
    """Why `actor` may not decide an approval on `incident`, or None."""
    allowed = approval_deciders(incident)
    if actor in allowed:
        return None
    if actor == _author(incident):
        return f"{actor} is the author of this change and may not approve it"
    if not allowed:
        return (
            f"{incident.service} has no approvers who may decide approvals "
            "(add approvers on the Services page)"
        )
    return f"{actor} is not allowed to decide approvals for {incident.service} ({allowed})"


def review_refusal(review: models.ReviewRequest, actor: str) -> str | None:
    """Why `actor` may not decide `review`, or None."""
    if actor == _author(review.incident):
        return f"{actor} is the author of this change and may not review it"
    if actor != review.reviewer:
        return f"{actor} is not allowed to decide this review (requested from {review.reviewer})"
    return None
