"""In-memory WorkManagementProvider (subphase 2.3).

The recording-fake counterpart of the live providers - the same role
integrations/fakes.py plays for the Phase 1 write transports and the
memory Slack/Notion clients play for theirs. It is the default
`work_provider` (helix/config.py), so a Helix box with no Jira/Linear/
Trello credentials still exercises the full lifecycle -> outbox ->
provider path, and it is the baseline arm of the 2.10 provider study.

`.items` is the whole store (external_id -> item dict, incident_id kept
per item for the dedup lookup) and `.calls` is the ordered log of
mutating ops - tests and the drain inspect both. `fail_with`, when set,
makes every method raise it: the outage simulation the drain retry
tests need (a provider outage must degrade delivery, never canonical
state).
"""

from typing import Any

from integrations.work.contract import (
    NEUTRAL_PRIORITIES,
    NEUTRAL_STATUSES,
    ProviderCapabilities,
    WorkItemError,
    WorkItemRef,
    WorkItemSpec,
)

#: The neutral link vocabulary from the contract - validates here so a
#: nonsense link type fails the same way on every provider.
NEUTRAL_LINK_TYPES = frozenset({"relates", "duplicates", "blocks"})


class MemoryWorkProvider:
    """Full-capability in-memory provider. `name` is the outbox
    `integration` value lifecycle_sync enqueues under."""

    name = "memory"

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None
        self._seq = 0

    def _gate(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def _get(self, external_id: str) -> dict[str, Any]:
        item = self.items.get(external_id)
        if item is None:
            raise WorkItemError(f"no work item {external_id!r}")
        return item

    @staticmethod
    def _ref(external_id: str, item: dict[str, Any]) -> WorkItemRef:
        return WorkItemRef(
            external_id=external_id,
            external_key=external_id,
            status=item["status"],
            assignee=item["assignee"],
            priority=item["priority"],
            raw=dict(item),
        )

    def _record(self, op: str, **fields: Any) -> None:
        self.calls.append({"op": op, **fields})

    def capabilities(self) -> ProviderCapabilities:
        self._gate()
        return ProviderCapabilities()

    def create_work_item(self, spec: WorkItemSpec) -> WorkItemRef:
        self._gate()
        if spec.priority is not None and spec.priority not in NEUTRAL_PRIORITIES:
            raise WorkItemError(f"unknown neutral priority {spec.priority!r}")
        # Idempotent create: a second create for the same incident hands
        # back the existing item rather than minting a duplicate (the
        # drain still consults find_by_incident first - this is the
        # provider-side belt to its suspender).
        for external_id, item in self.items.items():
            if item["incident_id"] == spec.incident_id:
                self._record("create_work_item", incident_id=spec.incident_id, deduplicated=True)
                return self._ref(external_id, item)
        self._seq += 1
        external_id = f"MEM-{self._seq}"
        self.items[external_id] = {
            "incident_id": spec.incident_id,
            "title": spec.title,
            "description": spec.description,
            "service": spec.service,
            "environment": spec.environment,
            "severity": spec.severity,
            "project": spec.project,
            "labels": list(spec.labels),
            "links": list(spec.links),
            "status": "open",
            "assignee": spec.assignee,
            "priority": spec.priority,
            "comments": [],
            "linked": [],
        }
        self._record("create_work_item", incident_id=spec.incident_id, external_id=external_id)
        return self._ref(external_id, self.items[external_id])

    def get_work_item(self, external_id: str) -> WorkItemRef | None:
        self._gate()
        item = self.items.get(external_id)
        return self._ref(external_id, item) if item is not None else None

    def find_by_incident(self, incident_id: str) -> WorkItemRef | None:
        self._gate()
        for external_id, item in self.items.items():
            if item["incident_id"] == incident_id:
                return self._ref(external_id, item)
        return None

    def update_work_item(
        self,
        external_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        priority: str | None = None,
    ) -> WorkItemRef:
        self._gate()
        if priority is not None and priority not in NEUTRAL_PRIORITIES:
            raise WorkItemError(f"unknown neutral priority {priority!r}")
        item = self._get(external_id)
        if title is not None:
            item["title"] = title
        if description is not None:
            item["description"] = description
        if priority is not None:
            item["priority"] = priority
        self._record("update_work_item", external_id=external_id)
        return self._ref(external_id, item)

    def assign_work_item(self, external_id: str, assignee: str | None) -> WorkItemRef:
        self._gate()
        item = self._get(external_id)
        item["assignee"] = assignee
        self._record("assign_work_item", external_id=external_id, assignee=assignee)
        return self._ref(external_id, item)

    def transition_work_item(self, external_id: str, status: str) -> WorkItemRef:
        self._gate()
        if status not in NEUTRAL_STATUSES:
            raise WorkItemError(f"unknown neutral status {status!r}")
        item = self._get(external_id)
        item["status"] = status
        self._record("transition_work_item", external_id=external_id, status=status)
        return self._ref(external_id, item)

    def comment_work_item(self, external_id: str, body: str, *, author: str = "helix") -> None:
        self._gate()
        self._get(external_id)["comments"].append({"author": author, "body": body})
        self._record("comment_work_item", external_id=external_id, author=author)

    def link_work_item(
        self, external_id: str, other_external_id: str, *, link_type: str = "relates"
    ) -> None:
        self._gate()
        if link_type not in NEUTRAL_LINK_TYPES:
            raise WorkItemError(f"unknown neutral link type {link_type!r}")
        self._get(other_external_id)
        self._get(external_id)["linked"].append({"other": other_external_id, "type": link_type})
        self._record(
            "link_work_item",
            external_id=external_id,
            other=other_external_id,
            link_type=link_type,
        )
