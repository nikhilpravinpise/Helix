"""GitHub payloads to canonical events (subphase E3).

Covers the three shapes Helix actually receives: check runs, pull request
reviews, and branch comparison state. GitHub's vocabulary - `conclusion`,
`CHANGES_REQUESTED`, `behind_by` - stops here.
"""

from typing import Any

from events.adapters._common import vendor_event
from events.schemas import (
    EngineeringEvent,
    EventSource,
    EventType,
    derive_event_id,
)

#: Check run conclusions worth an event. `neutral`, `skipped` and
#: `cancelled` are not failures; `action_required` is, because the check
#: is telling a human to intervene.
_CONCLUSIONS = {
    "failure": EventType.BUILD_FAILED,
    "timed_out": EventType.BUILD_FAILED,
    "action_required": EventType.BUILD_FAILED,
    "success": EventType.BUILD_PASSED,
}

_REVIEW_STATES = {
    "CHANGES_REQUESTED": EventType.PR_REJECTED,
    "APPROVED": EventType.PR_APPROVED,
}


def check_run_events(
    payload: dict[str, Any],
    service: str = "payment-api",
    environment: str = "sandbox",
) -> list[EngineeringEvent]:
    """Events for completed check runs on one ref."""
    ref = payload.get("ref") or payload.get("sha")
    out: list[EngineeringEvent] = []
    for item in payload.get("items") or []:
        if item.get("status") != "completed":
            continue
        event_type = _CONCLUSIONS.get(str(item.get("conclusion") or "").lower())
        if event_type is None:
            continue
        out.append(
            vendor_event(
                # A check run's url embeds its id and is stable across
                # redeliveries; the ref plus name is the fallback when a
                # payload omits it, and is unique per run on that commit.
                event_id=derive_event_id("github", item.get("url") or f"{ref}:{item.get('name')}"),
                event_type=event_type,
                source=EventSource.GITHUB,
                service=service,
                environment=environment,
                metadata={
                    "check": item.get("name"),
                    "conclusion": item.get("conclusion"),
                    "ref": ref,
                    "url": item.get("url"),
                    "started_at": item.get("started_at"),
                    "completed_at": item.get("completed_at"),
                },
            )
        )
    return out


def review_events(
    payload: dict[str, Any],
    service: str = "payment-api",
    environment: str = "sandbox",
) -> list[EngineeringEvent]:
    """Events for pull request reviews that decided something.

    `COMMENTED` and `DISMISSED` are skipped: neither changes whether the
    pull request can proceed, and an event per drive-by comment would
    drown the reviews that did.
    """
    number = payload.get("number")
    out: list[EngineeringEvent] = []
    for item in payload.get("items") or []:
        event_type = _REVIEW_STATES.get(str(item.get("state") or "").upper())
        if event_type is None:
            continue
        out.append(
            vendor_event(
                # A review id is immutable once submitted.
                event_id=derive_event_id("github", "review", item.get("id")),
                event_type=event_type,
                source=EventSource.GITHUB,
                service=service,
                environment=environment,
                # The reviewer is the actor: unlike everything else in
                # Phase 1, this event was caused by a person.
                actor=str(item.get("user") or "system"),
                metadata={
                    "pr": number,
                    "review_id": item.get("id"),
                    "state": item.get("state"),
                    "body": item.get("body"),
                    "submitted_at": item.get("submitted_at"),
                },
            )
        )
    return out


_PR_ACTIONS = {
    "opened": EventType.PR_OPENED,
    "reopened": EventType.PR_OPENED,
    "synchronize": EventType.PR_UPDATED,
    "edited": EventType.PR_UPDATED,
}


def pull_request_events(
    payload: dict[str, Any],
    service: str = "payment-api",
    environment: str = "sandbox",
) -> list[EngineeringEvent]:
    """Events for a pull request being opened or updated (subphase 2.1/2.9:
    CONTEXT.md section 16's trigger for the change-review lifecycle).

    `action` follows GitHub's own webhook vocabulary (`opened`,
    `reopened`, `synchronize`, `edited`); anything else (`closed`,
    `merged`, review-only actions the review-events adapter already
    covers) yields no event here. `files` - when the caller supplies it,
    e.g. from `GitHubEvidence.get_diff` - carries the changed paths
    Triage's code-ownership resolution needs; its absence is not an
    error, since 2.5's registry fallback covers a payload that only
    names the PR.
    """
    action = str(payload.get("action") or "").lower()
    event_type = _PR_ACTIONS.get(action)
    if event_type is None:
        return []
    number = payload.get("number")
    head = payload.get("head") or {}
    base = payload.get("base") or {}
    # The PR's author: `pull_request.user.login` in GitHub's schema (the
    # human whose change is under review - Review's self-review exclusion
    # keys on it). `sender.login` is who triggered the action, which for
    # synchronize can differ; author is the safer reading, with sender as
    # the fallback for payload shapes that only carry the trigger. A
    # top-level `user` never existed in this webhook - reading it meant
    # every real PR emitted actor="system", and self-review exclusion
    # silently stopped applying.
    pr_user = (payload.get("pull_request") or {}).get("user") or {}
    sender = payload.get("sender") or {}
    author = pr_user.get("login") or sender.get("login") or payload.get("user") or "system"
    return [
        vendor_event(
            # A PR's identity plus its head sha: re-synchronizing to a
            # new commit is a distinct, real update, not a redelivery.
            event_id=derive_event_id("github", "pr", number, head.get("sha")),
            event_type=event_type,
            source=EventSource.GITHUB,
            service=service,
            environment=environment,
            actor=str(author),
            metadata={
                "pr": number,
                "title": payload.get("title"),
                "branch": head.get("ref"),
                "sha": head.get("sha"),
                "base": base.get("ref"),
                "files": payload.get("files") or [],
                "action": action,
            },
        )
    ]


def branch_state_events(
    payload: dict[str, Any],
    service: str = "payment-api",
    environment: str = "sandbox",
) -> list[EngineeringEvent]:
    """An event when a branch has drifted from its base.

    Only `diverged` and `behind` qualify. `ahead` is the normal state of
    any branch with unmerged work on it, and `identical` is nothing at
    all.
    """
    comparison = str(payload.get("comparison") or "").lower()
    if comparison not in {"diverged", "behind"}:
        return []
    name, sha = payload.get("name"), payload.get("sha")
    return [
        vendor_event(
            # The branch plus its head sha: a branch that moves is a new
            # state worth a new event, while redelivering the same state
            # is not.
            event_id=derive_event_id("github", "branch", name, sha),
            event_type=EventType.BRANCH_DIVERGED,
            source=EventSource.GITHUB,
            service=service,
            environment=environment,
            metadata={
                "branch": name,
                "sha": sha,
                "base": payload.get("base"),
                "comparison": payload.get("comparison"),
                "ahead_by": payload.get("ahead_by"),
                "behind_by": payload.get("behind_by"),
            },
        )
    ]
