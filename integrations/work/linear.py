"""Linear implementation of the work-management contract (subphase 2.10).

Linear's GraphQL API (one POST to api.linear.app/graphql, the API key
as the Authorization header). The duplicate-prevention key is a
`helix-incident:<id>` marker line appended to the issue description -
`find_by_incident` is the server-side `description contains` filter, so
the drain's consult-before-create contract holds without local state,
and `update_work_item` preserves the marker so a description rewrite
cannot orphan the item.

Capability honesty (the point of 2.10): Linear has no blocked state
type, so `capabilities().states` is DERIVED from the team's actual
state list - a neutral word no state matches is undeclared and
`transition_work_item` raises WorkItemError for it. That is a declared
difference; an undeclared one would be a defect in the abstraction.

Every failure path raises WorkItemError - the single exception
vocabulary the outbox drain records.
"""

import re
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

LINEAR_API_URL = "https://api.linear.app/graphql"

#: The dedup marker embedded in issue descriptions (find_by_incident's
#: search term); preserved across description updates by
#: `_preserve_marker`.
_MARKER_PREFIX = "helix-incident:"
_MARKER_RE = re.compile(r"helix-incident:\S+")

#: Neutral status -> (acceptable state names, acceptable state types),
#: matched case-insensitively; names win over types, and a state named
#: for a different neutral word is never borrowed by type. `blocked`
#: has no type fallback because Linear has no blocked state type - only
#: a team-configured state literally named Blocked/On hold expresses it.
_NEUTRAL_TO_LINEAR: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "open": (
        ("backlog", "todo", "to do", "open", "new", "triage"),
        ("backlog", "unstarted", "triage"),
    ),
    "in_progress": (("in progress", "started", "doing"), ("started",)),
    "blocked": (("blocked", "on hold"), ()),
    "done": (("done", "completed", "complete"), ("completed",)),
    "closed": (("canceled", "cancelled", "closed"), ("canceled",)),
}

_FOREIGN_NAMES: dict[str, frozenset[str]] = {
    neutral: frozenset(
        name
        for other, (names, _) in _NEUTRAL_TO_LINEAR.items()
        if other != neutral
        for name in names
    )
    for neutral in _NEUTRAL_TO_LINEAR
}

#: Linear priorities are ints: 0 none, 1 urgent, 2 high, 3 normal, 4 low.
_LINEAR_PRIORITY = {"critical": 1, "high": 2, "medium": 3, "low": 4}
_LINEAR_PRIORITY_BACK = {0: None, 1: "critical", 2: "high", 3: "medium", 4: "low"}

_LINEAR_LINK_TYPES = {"relates": "related", "duplicates": "duplicate", "blocks": "blocks"}

_BLOCKED_STATE_NAMES = frozenset(_NEUTRAL_TO_LINEAR["blocked"][0])

_ISSUE_FIELDS = """
id identifier url title description priority
state { id name type }
assignee { id name displayName email }
labels { nodes { id name } }
"""

_TEAM_QUERY = """
query ($id: String!) {
  team(id: $id) {
    states { nodes { id name type } }
    members { nodes { id name displayName email } }
    labels { nodes { id name } }
  }
}
"""


def _neutral_status(state: dict[str, Any]) -> str:
    """Linear workflow-state -> neutral word. The state type carries the
    coarse truth; the name breaks the started (blocked) tie."""
    state_type = str(state.get("type") or "")
    name = str(state.get("name") or "").lower()
    if state_type == "completed":
        return "done"
    if state_type == "canceled":
        return "closed"
    if state_type == "started":
        return "blocked" if name in _BLOCKED_STATE_NAMES else "in_progress"
    return "open"  # backlog, unstarted, triage


def _preserve_marker(old_description: str, new_description: str) -> str:
    """Keep the dedup marker on a description rewrite - losing it would
    let a later find_by_incident miss and the drain mint a duplicate."""
    if _MARKER_RE.search(new_description):
        return new_description
    match = _MARKER_RE.search(old_description or "")
    return f"{new_description}\n\n{match.group(0)}" if match else new_description


class LinearProvider:
    """WorkManagementProvider over the Linear GraphQL API."""

    name = "linear"

    def __init__(
        self,
        api_key: str,
        team_id: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._team_id = team_id
        self._client = client or httpx.Client(timeout=timeout)
        self._team_data: dict[str, Any] | None = None
        self._caps: ProviderCapabilities | None = None

    # -- plumbing -----------------------------------------------------

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        try:
            response = self._client.post(
                LINEAR_API_URL,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": self._api_key},
            )
        except httpx.TimeoutException as exc:
            raise WorkItemError(f"linear timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise WorkItemError(f"linear transport error: {exc}") from exc
        try:
            raise_for_status(response)
        except EvidenceError as exc:
            raise WorkItemError(str(exc)) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise WorkItemError("linear returned a non-JSON response") from exc
        errors = data.get("errors") or []
        if errors:
            first = errors[0]
            raise WorkItemError(f"linear error: {first.get('message', first)}")
        return data.get("data") or {}

    # -- team data (states, members, labels) ---------------------------

    def _team(self) -> dict[str, Any]:
        """The team's states/members/labels, fetched once and cached -
        states drive both capabilities() and transition mapping."""
        if self._team_data is None:
            team = self._graphql(_TEAM_QUERY, {"id": self._team_id}).get("team")
            if team is None:
                raise WorkItemError(f"linear team {self._team_id!r} not found")
            self._team_data = team
        return self._team_data

    def _state_for(self, status: str) -> dict[str, Any] | None:
        names, types = _NEUTRAL_TO_LINEAR[status]
        states = (self._team().get("states") or {}).get("nodes") or []
        for state in states:
            if str(state.get("name") or "").lower() in names:
                return state
        foreign = _FOREIGN_NAMES[status]
        for state in states:
            if str(state.get("name") or "").lower() in foreign:
                continue
            if state.get("type") in types:
                return state
        return None

    def _member_id(self, assignee: str) -> str:
        wanted = assignee.lower()
        members = (self._team().get("members") or {}).get("nodes") or []
        for member in members:
            names = {
                str(member.get("name") or "").lower(),
                str(member.get("displayName") or "").lower(),
                str(member.get("email") or "").lower(),
            }
            if wanted in names:
                return str(member["id"])
        raise WorkItemError(f"no linear member matches {assignee!r}")

    def _label_ids(self, names: list[str]) -> list[str]:
        labels = (self._team().get("labels") or {}).setdefault("nodes", [])
        ids = []
        for name in names:
            found = next(
                (lab for lab in labels if str(lab.get("name") or "").lower() == name.lower()),
                None,
            )
            if found is None:
                data = self._graphql(
                    "mutation ($input: IssueLabelCreateInput!) { issueLabelCreate"
                    "(input: $input) { success issueLabel { id name } } }",
                    {"input": {"teamId": self._team_id, "name": name}},
                )
                found = (data.get("issueLabelCreate") or {}).get("issueLabel")
                if not found:
                    raise WorkItemError(f"linear could not create label {name!r}")
                labels.append(found)
            ids.append(found["id"])
        return ids

    # -- refs ----------------------------------------------------------

    @staticmethod
    def _ref(issue: dict[str, Any]) -> WorkItemRef:
        assignee = issue.get("assignee") or {}
        return WorkItemRef(
            external_id=str(issue.get("id") or ""),
            external_key=issue.get("identifier"),
            external_url=issue.get("url"),
            status=_neutral_status(issue.get("state") or {}),
            assignee=(assignee.get("displayName") or assignee.get("name") or assignee.get("email")),
            priority=_LINEAR_PRIORITY_BACK.get(issue.get("priority") or 0),
            raw=issue,
        )

    # -- contract ------------------------------------------------------

    def capabilities(self) -> ProviderCapabilities:
        """Derived from the team's real state list: the neutral words a
        Linear team can express are exactly the ones its states map to."""
        if self._caps is None:
            states = sorted(s for s in NEUTRAL_STATUSES if self._state_for(s) is not None)
            self._caps = ProviderCapabilities(states=states)
        return self._caps

    def create_work_item(self, spec: WorkItemSpec) -> WorkItemRef:
        marker = f"{_MARKER_PREFIX}{spec.incident_id}"
        description = f"{spec.description}\n\n{marker}" if spec.description else marker
        issue_input: dict[str, Any] = {
            "teamId": self._team_id,
            "title": spec.title,
            "description": description,
        }
        if spec.priority is not None:
            if spec.priority not in NEUTRAL_PRIORITIES:
                raise WorkItemError(f"unknown neutral priority {spec.priority!r}")
            issue_input["priority"] = _LINEAR_PRIORITY[spec.priority]
        if spec.labels:
            issue_input["labelIds"] = self._label_ids(spec.labels)
        if spec.assignee:
            issue_input["assigneeId"] = self._member_id(spec.assignee)
        data = self._graphql(
            "mutation ($input: IssueCreateInput!) { issueCreate(input: $input) "
            "{ success issue { " + _ISSUE_FIELDS + " } } }",
            {"input": issue_input},
        )
        issue = (data.get("issueCreate") or {}).get("issue")
        if not issue:
            raise WorkItemError("linear issueCreate returned no issue")
        return self._ref(issue)

    def get_work_item(self, external_id: str) -> WorkItemRef | None:
        data = self._graphql(
            "query ($id: String!) { issue(id: $id) { " + _ISSUE_FIELDS + " } }",
            {"id": external_id},
        )
        issue = data.get("issue")
        return self._ref(issue) if issue else None

    def find_by_incident(self, incident_id: str) -> WorkItemRef | None:
        data = self._graphql(
            "query ($term: String!) { issues(filter: { description: { contains:"
            " $term } }, first: 1) { nodes { " + _ISSUE_FIELDS + " } } }",
            {"term": f"{_MARKER_PREFIX}{incident_id}"},
        )
        nodes = (data.get("issues") or {}).get("nodes") or []
        return self._ref(nodes[0]) if nodes else None

    def _update(self, external_id: str, issue_input: dict[str, Any]) -> WorkItemRef:
        if not issue_input:
            ref = self.get_work_item(external_id)
            if ref is None:
                raise WorkItemError(f"linear issue {external_id!r} not found")
            return ref
        data = self._graphql(
            "mutation ($id: String!, $input: IssueUpdateInput!) { issueUpdate"
            "(id: $id, input: $input) { success issue { " + _ISSUE_FIELDS + " } } }",
            {"id": external_id, "input": issue_input},
        )
        issue = (data.get("issueUpdate") or {}).get("issue")
        if not issue:
            raise WorkItemError(f"linear issue {external_id!r} not found")
        return self._ref(issue)

    def update_work_item(
        self,
        external_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        priority: str | None = None,
    ) -> WorkItemRef:
        issue_input: dict[str, Any] = {}
        if title is not None:
            issue_input["title"] = title
        if description is not None:
            current = self.get_work_item(external_id)
            if current is None:
                raise WorkItemError(f"linear issue {external_id!r} not found")
            issue_input["description"] = _preserve_marker(
                str(current.raw.get("description") or ""), description
            )
        if priority is not None:
            if priority not in NEUTRAL_PRIORITIES:
                raise WorkItemError(f"unknown neutral priority {priority!r}")
            issue_input["priority"] = _LINEAR_PRIORITY[priority]
        return self._update(external_id, issue_input)

    def assign_work_item(self, external_id: str, assignee: str | None) -> WorkItemRef:
        return self._update(
            external_id,
            {"assigneeId": self._member_id(assignee) if assignee else None},
        )

    def transition_work_item(self, external_id: str, status: str) -> WorkItemRef:
        if status not in NEUTRAL_STATUSES:
            raise WorkItemError(f"unknown neutral status {status!r}")
        state = self._state_for(status)
        if state is None:
            raise WorkItemError(
                f"linear team has no state for {status!r} - an undeclared capability gap"
            )
        return self._update(external_id, {"stateId": state["id"]})

    def comment_work_item(self, external_id: str, body: str, *, author: str = "helix") -> None:
        data = self._graphql(
            "mutation ($input: CommentCreateInput!) { commentCreate(input: $input) { success } }",
            {"input": {"issueId": external_id, "body": body}},
        )
        if not (data.get("commentCreate") or {}).get("success"):
            raise WorkItemError(f"linear could not comment on {external_id!r}")

    def link_work_item(
        self, external_id: str, other_external_id: str, *, link_type: str = "relates"
    ) -> None:
        relation = _LINEAR_LINK_TYPES.get(link_type)
        if relation is None:
            raise WorkItemError(f"unknown neutral link type {link_type!r}")
        data = self._graphql(
            "mutation ($input: IssueRelationCreateInput!) { issueRelationCreate"
            "(input: $input) { success } }",
            {
                "input": {
                    "issueId": external_id,
                    "relatedIssueId": other_external_id,
                    "type": relation,
                }
            },
        )
        if not (data.get("issueRelationCreate") or {}).get("success"):
            raise WorkItemError(f"linear could not link {external_id!r} -> {other_external_id!r}")
