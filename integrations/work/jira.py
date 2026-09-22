"""Jira implementation of the work-management contract (subphase 2.3).

Jira REST v3 over httpx, basic auth (email + API token). The duplicate-
prevention key (the 2.3 exit criterion's "exactly one issue per
incident") is a `helix-incident-<id>` label applied at create time:
`find_by_incident` is the JQL `labels = "helix-incident-<id>"` lookup
the drain consults before every create, so a retried or re-enqueued
create can never mint a second issue for one incident.

Neutral-vocabulary mapping (CONTEXT.md section 11):

- status: Jira has no portable status model, only per-workflow
  transitions. `transition_work_item` reads the issue's available
  transitions and picks the one whose name or target statusCategory
  matches the neutral word (`_NEUTRAL_TO_JIRA`); a workflow that cannot
  express it raises WorkItemError rather than guessing. Reads map back
  through statusCategory (new -> open, indeterminate -> in_progress,
  done -> done) with the status name breaking the blocked/closed ties
  the category model cannot express.
- priority: the neutral four map onto Jira's Lowest..Highest names.
- assignee: name-based assignment; None clears.

Every failure path raises WorkItemError - the single exception
vocabulary the outbox drain records, mirroring EvidenceError on the
evidence read side.
"""

from typing import Any

import httpx

from integrations.evidence import EvidenceError
from integrations.http import raise_for_status
from integrations.work.contract import (
    NEUTRAL_PRIORITIES,
    NEUTRAL_STATUSES,
    ProviderCapabilities,
    WorkItemError,
    WorkItemRef,
    WorkItemSpec,
)

#: Neutral status -> (acceptable transition/status names, acceptable
#: target statusCategory keys), matched case-insensitively. Names win
#: over categories (a "Closed" transition beats an unrelated "Done");
#: a transition named for a DIFFERENT neutral word is never borrowed by
#: category. `closed` is the one exception: Jira's done category cannot
#: distinguish resolved from closed, so done-typed targets (a "Done"
#: transition) are a legitimate fallback.
_NEUTRAL_TO_JIRA: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "open": (
        ("to do", "todo", "open", "reopen", "backlog", "new"),
        ("new",),
    ),
    "in_progress": (
        ("in progress", "in-progress", "doing", "start progress"),
        ("indeterminate",),
    ),
    "blocked": (("blocked", "block", "on hold"), ()),
    "done": (
        ("done", "resolve", "resolved", "complete", "completed"),
        ("done",),
    ),
    "closed": (
        ("closed", "close", "cancelled", "canceled", "won't do", "wont do"),
        ("done",),
    ),
}

_FOREIGN_NAMES: dict[str, frozenset[str]] = {
    neutral: frozenset(
        name for other, (names, _) in _NEUTRAL_TO_JIRA.items() if other != neutral for name in names
    )
    for neutral in _NEUTRAL_TO_JIRA
}

_JIRA_PRIORITY = {"low": "Low", "medium": "Medium", "high": "High", "critical": "Highest"}
_JIRA_PRIORITY_BACK = {
    "lowest": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "highest": "critical",
}

_JIRA_LINK_TYPES = {"relates": "Relates", "duplicates": "Duplicate", "blocks": "Blocks"}

_CLOSED_STATUS_NAMES = frozenset(_NEUTRAL_TO_JIRA["closed"][0])
_BLOCKED_STATUS_NAMES = frozenset(_NEUTRAL_TO_JIRA["blocked"][0])


def _adf(text: str) -> dict[str, Any]:
    """Plain text as an Atlassian Document Format doc - Jira v3 rejects
    a bare string description."""
    doc: dict[str, Any] = {"type": "doc", "version": 1, "content": []}
    if text:
        doc["content"].append({"type": "paragraph", "content": [{"type": "text", "text": text}]})
    return doc


def _neutral_status(status: dict[str, Any]) -> str:
    """Jira status -> neutral word. statusCategory carries the coarse
    truth; the status name breaks the indeterminate (blocked) and done
    (closed) ties the category model cannot express."""
    name = str(status.get("name") or "").lower()
    category = str((status.get("statusCategory") or {}).get("key") or "")
    if category == "done":
        return "closed" if name in _CLOSED_STATUS_NAMES else "done"
    if category == "indeterminate":
        return "blocked" if name in _BLOCKED_STATUS_NAMES else "in_progress"
    return "open"


class JiraProvider:
    """WorkManagementProvider over Jira REST v3."""

    name = "jira"

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        project_key: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = (email, api_token)
        self._project_key = project_key
        self._client = client or httpx.Client(auth=self._auth, timeout=timeout)

    # -- plumbing: one exception vocabulary -------------------------

    def _raw(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(
                method, f"{self._base_url}{path}", auth=self._auth, **kwargs
            )
        except httpx.TimeoutException as exc:
            raise WorkItemError(f"jira timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise WorkItemError(f"jira transport error: {exc}") from exc

    @staticmethod
    def _check(response: httpx.Response) -> None:
        try:
            raise_for_status(response)
        except EvidenceError as exc:
            raise WorkItemError(str(exc)) from exc

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._raw(method, path, **kwargs)
        self._check(response)
        return response

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise WorkItemError("jira returned a non-JSON response") from exc

    # -- refs ---------------------------------------------------------

    def _ref(self, issue: dict[str, Any]) -> WorkItemRef:
        fields = issue.get("fields") or {}
        assignee = fields.get("assignee") or {}
        priority = (fields.get("priority") or {}).get("name")
        key = issue.get("key")
        return WorkItemRef(
            external_id=str(issue.get("id") or key or ""),
            external_key=key,
            external_url=f"{self._base_url}/browse/{key}" if key else None,
            status=_neutral_status(fields.get("status") or {}),
            assignee=assignee.get("displayName") or assignee.get("name"),
            priority=_JIRA_PRIORITY_BACK.get(str(priority).lower()) if priority else None,
            raw=issue,
        )

    def _require(self, external_id: str) -> WorkItemRef:
        """Refetch after a write - the mutating endpoints answer 204, so
        the returned ref always reflects Jira's own state."""
        ref = self.get_work_item(external_id)
        if ref is None:
            raise WorkItemError(f"jira issue {external_id!r} not found after write")
        return ref

    # -- contract ---------------------------------------------------

    def capabilities(self) -> ProviderCapabilities:
        # Jira workflows are per-project admin configuration, so the
        # full vocabulary is declared statically; a workflow that cannot
        # express a neutral status raises WorkItemError in
        # _pick_transition at the call.
        return ProviderCapabilities()

    def create_work_item(self, spec: WorkItemSpec) -> WorkItemRef:
        fields: dict[str, Any] = {
            "project": {"key": spec.project or self._project_key},
            "summary": spec.title,
            "description": _adf(spec.description),
            "issuetype": {"name": "Task"},
            "labels": [f"helix-incident-{spec.incident_id}", *spec.labels],
        }
        if spec.priority is not None:
            if spec.priority not in NEUTRAL_PRIORITIES:
                raise WorkItemError(f"unknown neutral priority {spec.priority!r}")
            fields["priority"] = {"name": _JIRA_PRIORITY[spec.priority]}
        if spec.assignee:
            fields["assignee"] = {"name": spec.assignee}
        data = self._json(self._request("POST", "/rest/api/3/issue", json={"fields": fields}))
        key = data.get("key")
        return WorkItemRef(
            external_id=str(data.get("id") or key or ""),
            external_key=key,
            external_url=f"{self._base_url}/browse/{key}" if key else None,
            status="open",
            assignee=spec.assignee,
            priority=spec.priority,
            raw=data,
        )

    def get_work_item(self, external_id: str) -> WorkItemRef | None:
        response = self._raw("GET", f"/rest/api/3/issue/{external_id}")
        if response.status_code == 404:
            return None
        self._check(response)
        return self._ref(self._json(response))

    def find_by_incident(self, incident_id: str) -> WorkItemRef | None:
        jql = f'project = "{self._project_key}" AND labels = "helix-incident-{incident_id}"'
        data = self._json(
            self._request("GET", "/rest/api/3/search", params={"jql": jql, "maxResults": "2"})
        )
        issues = data.get("issues") or []
        return self._ref(issues[0]) if issues else None

    def update_work_item(
        self,
        external_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        priority: str | None = None,
    ) -> WorkItemRef:
        fields: dict[str, Any] = {}
        if title is not None:
            fields["summary"] = title
        if description is not None:
            fields["description"] = _adf(description)
        if priority is not None:
            if priority not in NEUTRAL_PRIORITIES:
                raise WorkItemError(f"unknown neutral priority {priority!r}")
            fields["priority"] = {"name": _JIRA_PRIORITY[priority]}
        if fields:
            self._request("PUT", f"/rest/api/3/issue/{external_id}", json={"fields": fields})
        return self._require(external_id)

    def assign_work_item(self, external_id: str, assignee: str | None) -> WorkItemRef:
        self._request(
            "PUT",
            f"/rest/api/3/issue/{external_id}/assignee",
            json={"name": assignee},
        )
        return self._require(external_id)

    def _pick_transition(self, transitions: list[dict[str, Any]], status: str) -> dict[str, Any]:
        names, categories = _NEUTRAL_TO_JIRA[status]
        foreign = _FOREIGN_NAMES[status]
        if status == "closed":
            # done-typed targets are a legitimate closed fallback (a
            # Closed/Done workflow), not a foreign-name borrow.
            foreign = foreign - frozenset(_NEUTRAL_TO_JIRA["done"][0])

        def targets(transition: dict[str, Any]) -> set[str]:
            return {
                str(transition.get("name") or "").lower(),
                str((transition.get("to") or {}).get("name") or "").lower(),
            }

        for transition in transitions:
            if targets(transition) & set(names):
                return transition
        for transition in transitions:
            if targets(transition) & foreign:
                continue
            category = str(
                ((transition.get("to") or {}).get("statusCategory") or {}).get("key") or ""
            )
            if category in categories:
                return transition
        raise WorkItemError(f"jira workflow offers no transition for {status!r}")

    def transition_work_item(self, external_id: str, status: str) -> WorkItemRef:
        if status not in NEUTRAL_STATUSES:
            raise WorkItemError(f"unknown neutral status {status!r}")
        data = self._json(self._request("GET", f"/rest/api/3/issue/{external_id}/transitions"))
        transition = self._pick_transition(data.get("transitions") or [], status)
        self._request(
            "POST",
            f"/rest/api/3/issue/{external_id}/transitions",
            json={"transition": {"id": transition["id"]}},
        )
        return self._require(external_id)

    def comment_work_item(self, external_id: str, body: str, *, author: str = "helix") -> None:
        self._request(
            "POST",
            f"/rest/api/3/issue/{external_id}/comment",
            json={"body": _adf(body)},
        )

    def link_work_item(
        self, external_id: str, other_external_id: str, *, link_type: str = "relates"
    ) -> None:
        name = _JIRA_LINK_TYPES.get(link_type)
        if name is None:
            raise WorkItemError(f"unknown neutral link type {link_type!r}")
        self._request(
            "POST",
            "/rest/api/3/issueLink",
            json={
                "type": {"name": name},
                "outwardIssue": {"key": external_id},
                "inwardIssue": {"key": other_external_id},
            },
        )
