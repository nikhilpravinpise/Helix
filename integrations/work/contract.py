"""The provider-neutral work-management contract (subphase 2.3/2.10).

CONTEXT.md section 11: Jira, Linear and Trello sit behind ONE contract,
and agents use only the neutral vocabulary below - `WorkItemSpec`,
`WorkItemRef`, `ProviderCapabilities` - never a vendor noun. The 2.10
provider study is the proof this boundary is real: identical lifecycle
outcomes across providers with zero agent-code changes, differences
surfacing only through declared capabilities.

## Provider-neutral status vocabulary

`WorkItemRef.status` and `transition_work_item` speak one vocabulary:
`open`, `in_progress`, `blocked`, `done`, `closed`. A provider that
natively lacks a state (Trello has no workflow beyond list membership)
maps what it can and declares the rest in `ProviderCapabilities.states`.
Callers never branch on the provider name - they consult
`capabilities()` when they need to know a degradation exists.
"""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

#: The only status vocabulary callers may use (CONTEXT.md section 11's
#: "Status" concept made concrete). `open` is the state a freshly created
#: item starts in; `closed` is a terminal state distinct from `done` so
#: "finished successfully" and "will not be worked" stay separable.
NEUTRAL_STATUSES = frozenset({"open", "in_progress", "blocked", "done", "closed"})

#: The only priority vocabulary callers may use. Providers without
#: native priority (Trello) declare `capabilities().priority == False`
#: and accept-and-ignore it rather than erroring.
NEUTRAL_PRIORITIES = frozenset({"low", "medium", "high", "critical"})


class WorkItemError(RuntimeError):
    """A provider call failed. Raised by every implementation for auth,
    transport, and API-level failures so the outbox drain has one
    exception vocabulary to record - the way `EvidenceError` is the one
    vocabulary evidence capabilities share."""


class WorkItemSpec(BaseModel):
    """What to create. `incident_id` is the Helix INC-* external id and
    doubles as the duplicate-prevention key: providers index it (Jira
    label, Linear/Trello description marker) so `find_by_incident` can
    answer "is there already an item for this incident" before any
    create."""

    incident_id: str
    title: str
    description: str = ""
    service: str | None = None
    environment: str | None = None
    severity: str | None = None
    priority: str | None = None  # one of NEUTRAL_PRIORITIES
    assignee: str | None = None
    project: str | None = None
    labels: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)  # e.g. PR URLs, dedup refs


class WorkItemRef(BaseModel):
    """A provider's answer about one work item, in neutral vocabulary.
    `raw` keeps the provider-native payload for debugging and audit -
    callers must not read vendor fields out of it for decisions."""

    external_id: str
    external_key: str | None = None  # human key, e.g. "HELIX-123"
    external_url: str | None = None
    status: str = "open"  # one of NEUTRAL_STATUSES
    assignee: str | None = None
    priority: str | None = None
    title: str | None = None  # the provider's own title, when it returns one
    project: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ProviderCapabilities(BaseModel):
    """What a provider can express, in contract terms. The 2.10 exit
    criterion: where a provider genuinely lacks a capability (Trello has
    no native priority or workflow states), the gap is declared HERE and
    asserted by a test - never silently approximated in agent code."""

    priority: bool = True
    assignee: bool = True
    states: list[str] = sorted(NEUTRAL_STATUSES)
    links: bool = True
    comments: bool = True
    labels: bool = True


@runtime_checkable
class WorkManagementProvider(Protocol):
    """The contract every work-management provider implements.

    Implementations: `memory.MemoryWorkProvider` (fixture/tests),
    `jira.JiraProvider`, `linear.LinearProvider`, `trello.TrelloProvider`.
    Every method raises `WorkItemError` on failure - never a vendor
    exception type - so the drain's error ledger stays provider-neutral.
    """

    name: str

    def capabilities(self) -> ProviderCapabilities:
        """This provider's declared capability set (2.10)."""
        ...

    def create_work_item(self, spec: WorkItemSpec) -> WorkItemRef:
        """Create one item. Callers guarantee dedup by consulting
        `find_by_incident` first; a provider may still return the
        existing item rather than a duplicate if its API supports
        idempotent create."""
        ...

    def get_work_item(self, external_id: str) -> WorkItemRef | None:
        """Fetch one item by provider id, or None if it does not exist."""
        ...

    def find_by_incident(self, incident_id: str) -> WorkItemRef | None:
        """The item already tracking `incident_id`, or None - the
        duplicate-prevention lookup (2.3)."""
        ...

    def update_work_item(
        self,
        external_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        priority: str | None = None,
    ) -> WorkItemRef:
        """Patch mutable fields; None leaves a field untouched."""
        ...

    def assign_work_item(self, external_id: str, assignee: str | None) -> WorkItemRef:
        """Set or clear (None) the assignee."""
        ...

    def transition_work_item(self, external_id: str, status: str) -> WorkItemRef:
        """Move the item to a NEUTRAL_STATUSES status. Providers map the
        neutral word onto their native workflow; a status outside the
        provider's declared `capabilities().states` raises
        `WorkItemError` so undeclared differences surface as defects."""
        ...

    def comment_work_item(self, external_id: str, body: str, *, author: str = "helix") -> None:
        """Append one comment; the author string is informational."""
        ...

    def link_work_item(
        self, external_id: str, other_external_id: str, *, link_type: str = "relates"
    ) -> None:
        """Relate two items (e.g. duplicate-of). `link_type` is neutral:
        `relates` | `duplicates` | `blocks`."""
        ...
