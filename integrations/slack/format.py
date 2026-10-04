"""What an incident says in Slack: readable cards, real people, links.

Before this every message was `INC-x: -> received - new recovery incident`,
with no account of what broke, no links, and nobody pinged. This module
turns an incident's own evidence into a one-line description, mentions the
people the service registry names (by Slack id when the registry has one,
by name otherwise), and links what a responder would open next: the PR,
the Jenkins build, the Jira ticket, the Helix incident page.

Everything here is a pure function over plain attributes, so the outbox
drain renders with it and tests exercise it without Slack.
"""

from dataclasses import dataclass, field
from typing import Any

#: How each status reads in a thread update. `ping` marks the moments a
#: human is needed, where the owner is mentioned rather than just informed.
_STATUS = {
    "received": ("📥", "Received", False),
    "triaged": ("🧭", "Triaged", False),
    "owner_assigned": ("👤", "Owner assigned", False),
    "review_pending": ("👀", "Waiting for review", False),
    "approved": ("👍", "Change approved", False),
    "rejected": ("✋", "Changes requested", False),
    "fix_required": ("🔧", "Waiting for a fix", False),
    "deploying": ("🚀", "Deploying", False),
    "diagnosing": ("🔍", "Diagnosing", False),
    "remediation_pending": ("🧠", "Planning a fix", False),
    "validating": ("🧪", "Checking the plan", False),
    "awaiting_approval": ("⏸️", "Waiting for approval", True),
    "executing": ("🛠️", "Applying the fix", False),
    "verifying": ("🔎", "Verifying", False),
    "recovered": ("✅", "Recovered", False),
    "escalated": ("🚨", "Escalated to a human", True),
    "failed": ("❌", "Run failed", True),
}

_SEVERITY_ICON = {"critical": "🔴", "high": "🔴", "medium": "🟠", "low": "🟡"}


@dataclass
class IncidentView:
    """The facts a Slack message needs, read off one incident."""

    incident_id: str
    service: str
    environment: str
    severity: str
    kind: str
    owner: str | None
    summary: str
    links: list[tuple[str, str]] = field(default_factory=list)


def mention(handle: str | None, people: dict[str, Any]) -> str:
    """`<@U...>` for someone with a Slack id, their bold name otherwise."""
    if not handle:
        return "_unassigned_"
    person = people.get(handle)
    if person is not None and getattr(person, "slack", None):
        return f"<@{person.slack}>"
    name = getattr(person, "name", None) or handle
    return f"*{name}*"


def _display(handle: str | None, people: dict[str, Any]) -> str:
    person = people.get(handle or "")
    return getattr(person, "name", None) or handle or "someone"


def _facts(incident: Any) -> tuple[str, dict[str, Any], str]:
    """The source, merged fields, and actor of the incident's newest piece
    of external evidence - a Phase 1 signal or a vendor event record."""
    records = [r for r in incident.event_records if r.source != "helix"]
    if records:
        record = records[-1]
        envelope = record.envelope or {}
        meta = dict(envelope.get("metadata") or {})
        meta.update(meta.pop("signature", None) or {})
        meta.update(meta.pop("raw", None) or {})
        meta["_type"] = record.event_type
        return record.source, meta, str(envelope.get("actor") or "")
    if incident.signals:
        signal = incident.signals[-1]
        return signal.source, {**(signal.raw or {}), **(signal.signature or {})}, ""
    return "", {}, ""


def describe_incident(
    incident: Any, *, repo: str, ui_base: str, people: dict[str, Any] | None = None
) -> IncidentView:
    """One sentence on what happened, plus the links worth opening."""
    people = people or {}
    source, facts, actor = _facts(incident)
    links: list[tuple[str, str]] = []

    if facts.get("pr") is not None:
        number = facts["pr"]
        title = facts.get("title") or "untitled"
        verb = "updated" if facts.get("_type") == "pr.updated" else "opened"
        summary = f"PR #{number} *{title}* {verb} by {_display(actor, people)}"
        if facts.get("branch"):
            summary += f" on `{facts['branch']}`"
        links.append((f"PR #{number}", f"https://github.com/{repo}/pull/{number}"))
    elif source == "jenkins" or facts.get("stage"):
        job = facts.get("job") or incident.service
        build = facts.get("build_number")
        label = f"Jenkins build #{build}" if build is not None else "A Jenkins build"
        summary = f"{label} of `{job}` failed at stage *{facts.get('stage') or 'unknown'}*"
        if facts.get("build_url"):
            links.append((label, str(facts["build_url"])))
    elif facts.get("waiting_reason") or facts.get("pod"):
        pod = facts.get("pod") or incident.service
        reason = facts.get("waiting_reason")
        if reason:
            summary = f"Pod `{pod}` is in *{reason}*"
            detail = []
            if facts.get("terminated_reason"):
                detail.append(f"last exit: {facts['terminated_reason']}")
            if facts.get("restart_count"):
                detail.append(f"{facts['restart_count']} restarts")
            if detail:
                summary += f" ({', '.join(detail)})"
        else:
            summary = f"Pod `{pod}` is running but not Ready"
    else:
        what = facts.get("_type") or "a failure signal"
        summary = f"{what} from {source or 'an unknown source'}"

    for item in incident.work_items:
        if getattr(item, "external_url", None):
            key = item.external_key or "ticket"
            links.append((f"Jira {key}", item.external_url))
    links.append(
        (f"Helix {incident.incident_id}", f"{ui_base}/ui/incidents/{incident.incident_id}")
    )

    kind = getattr(incident.kind, "value", incident.kind)
    return IncidentView(
        incident_id=incident.incident_id,
        service=incident.service,
        environment=incident.environment,
        severity=str(incident.severity),
        kind=str(kind),
        owner=incident.owner,
        summary=summary,
        links=links,
    )


def _links_line(view: IncidentView) -> str:
    return "  ·  ".join(f"<{url}|{label}>" for label, url in view.links)


def incident_card(view: IncidentView, people: dict[str, Any]) -> list[dict[str, Any]]:
    """The thread root: what broke, how bad, who owns it, where to look."""
    icon = _SEVERITY_ICON.get(view.severity.lower(), "⚪")
    kind = "Change" if view.kind == "change" else "Incident"
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{icon} {kind} {view.incident_id} · {view.service}",
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": view.summary}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Severity*\n{view.severity}"},
                {"type": "mrkdwn", "text": f"*Environment*\n{view.environment}"},
                {"type": "mrkdwn", "text": f"*Owner*\n{mention(view.owner, people)}"},
                {"type": "mrkdwn", "text": f"*Kind*\n{view.kind}"},
            ],
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _links_line(view)}]},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Helix posts every step in this thread."}],
        },
    ]


def update_text(status: str, cause: str, *, owner: str | None, people: dict[str, Any]) -> str:
    """One readable thread line per transition; the owner is mentioned only
    when a human is actually needed."""
    icon, label, ping = _STATUS.get(status, ("•", status.replace("_", " "), False))
    line = f"{icon} *{label}*"
    if cause:
        line += f" - {cause}"
    if ping:
        line += f"\n{mention(owner, people)} this needs you."
    return line


def _buttons(block_id: str, yes: tuple[str, str], no: tuple[str, str], row_id: int) -> dict:
    return {
        "type": "actions",
        "block_id": block_id,
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": yes[0]},
                "style": "primary",
                "action_id": yes[1],
                "value": str(row_id),
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": no[0]},
                "style": "danger",
                "action_id": no[1],
                "value": str(row_id),
            },
        ],
    }


def review_card(
    view: IncidentView, *, review_id: int, reviewer: str, rationale: str, people: dict[str, Any]
) -> list[dict[str, Any]]:
    """Asks one reviewer for a decision, says why them, links the PR."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"👀 {mention(reviewer, people)}, please review: {view.summary}",
            },
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Why you: {rationale}"}]},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _links_line(view)}]},
        _buttons(
            f"helix_review:{review_id}",
            ("Approve", f"helix_review_approve:{review_id}"),
            ("Request changes", f"helix_review_reject:{review_id}"),
            review_id,
        ),
    ]


def approval_card(
    view: IncidentView, *, approval_id: int, action: str, reason: str, people: dict[str, Any]
) -> list[dict[str, Any]]:
    """Asks the owner to allow one action, and says why Helix had to ask."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"✋ {mention(view.owner, people)}, Helix wants to run `{action}` on "
                    f"*{view.service}* ({view.environment}) for {view.incident_id}."
                ),
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*What happened:* {view.summary}"}},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Why it needs approval: {reason or 'policy'}"}
            ],
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _links_line(view)}]},
        _buttons(
            f"helix_approval:{approval_id}",
            ("Approve", f"helix_approve:{approval_id}"),
            ("Reject", f"helix_reject:{approval_id}"),
            approval_id,
        ),
    ]
