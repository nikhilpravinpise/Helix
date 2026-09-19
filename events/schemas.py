"""The canonical EngineeringEvent envelope (subphase E1).

One event shape, defined and validated, before any broker exists. The
envelope is CONTEXT.md section 7's, with `schema_version` added because
plan.md E1 requires a versioning field and a stated compatibility rule.

Nothing here knows about Kafka, and nothing here imports from `state` or
`agent`: events are the vocabulary those layers speak, so the dependency
runs one way only.

## The taxonomy discrepancy, decided

CONTEXT.md section 7 and the idea document disagree on the runtime
family. plan.md E1 requires that this be decided once, recorded, and kept
single-sourced. This module is that single source.

The test applied to each candidate was the same: does a signal Helix
actually handles have no honest label without it?

- **`rollout.failed` - rejected.** The two scenarios that would want it,
  `failed-rollout` and `config-mismatch`, are already covered.
  `failed-rollout` reports `ImagePullBackOff` on the pod, which is
  `pod.failed`; `config-mismatch` reports `rollout_wedged` with the old
  ReplicaSet still serving, which is `deployment.failed`. CONTEXT.md
  section 7 says to expand only when a real workflow needs another event,
  and neither does.

- **`service.unhealthy` - accepted.** The recorded `dependency-failure`
  fault leaves the pod `Running` with no restarts, failing readiness
  because its downstream was scaled to zero. Nothing died and nothing was
  rolling out, so both `pod.failed` and `deployment.failed` would be
  false. No Phase 1 signal produces it yet - it has no classifier rule in
  `events.classify` - but the fault is real and recorded, and the E3
  Kubernetes adapter is where it starts arriving.

- **`branch.diverged` - added.** Not part of the original discrepancy,
  and forced by evidence: the `stale-branch` scenario signals
  `{type: branch_state, comparison: diverged, behind_by: 14}`, and the
  taxonomy's git family is entirely push and pull-request events. Helix
  recovers this scenario today, so without it a working scenario has no
  label at all and would have to be filed as a PR event that never
  happened.

## Compatibility rule

`schema_version` is `MAJOR.MINOR`.

- **MINOR** covers additive, backward-compatible change: a new optional
  envelope field, a new member of `EventType`, a new `metadata` key. A
  consumer written against an older MINOR keeps working.
- **MAJOR** covers anything else: removing or renaming a field, changing
  its type, or changing what an existing `event_type` means.

Two obligations on consumers, so that MINOR really is backward
compatible:

1. Unknown `metadata` keys must be ignored, never treated as an error.
   `metadata` is deliberately open.
2. An `event_type` a consumer does not handle must be skipped, not
   failed on. A consumer that rejects unknown types turns every additive
   taxonomy change into a breaking one.

The envelope itself is closed (`extra="forbid"`): an unrecognised
top-level field is a producer bug, and silently accepting it would let a
misspelled `servcie` through as free-form data.
"""

import secrets
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Bumped per the compatibility rule above.
SCHEMA_VERSION = "1.0"


class EventSource(StrEnum):
    """The system an event came from.

    `HELIX` is Helix itself: E5 emits `incident.created`,
    `remediation.requested` and the rest from here, and those events have
    no vendor origin.
    """

    KUBERNETES = "kubernetes"
    JENKINS = "jenkins"
    GITHUB = "github"
    ELK = "elk"
    HELIX = "helix"


class EventType(StrEnum):
    """The starting taxonomy: CONTEXT.md section 7, plus the two runtime
    and git additions decided in this module's docstring."""

    # Source control
    PUSH_CREATED = "push.created"
    PR_OPENED = "pr.opened"
    PR_UPDATED = "pr.updated"
    PR_APPROVED = "pr.approved"
    PR_REJECTED = "pr.rejected"
    PR_MERGED = "pr.merged"
    BRANCH_DIVERGED = "branch.diverged"

    # Continuous integration
    BUILD_STARTED = "build.started"
    BUILD_PASSED = "build.passed"
    BUILD_FAILED = "build.failed"

    # Deployment and runtime
    DEPLOYMENT_STARTED = "deployment.started"
    DEPLOYMENT_FAILED = "deployment.failed"
    DEPLOYMENT_SUCCEEDED = "deployment.succeeded"
    POD_FAILED = "pod.failed"
    SERVICE_UNHEALTHY = "service.unhealthy"

    # Helix's own lifecycle (emitted in E5)
    INCIDENT_CREATED = "incident.created"
    REMEDIATION_REQUESTED = "remediation.requested"
    APPROVAL_REQUESTED = "approval.requested"
    REMEDIATION_COMPLETED = "remediation.completed"
    INCIDENT_RESOLVED = "incident.resolved"


def new_event_id() -> str:
    """`evt_<12 hex>`, matching CONTEXT.md section 7's `evt_123` shape.

    Random for now. E3 makes ids derived rather than random, so that a
    redelivered vendor webhook yields the same id; that needs the vendor
    payload's own identity, which does not exist yet at E1.
    """
    return f"evt_{secrets.token_hex(6)}"


class EngineeringEvent(BaseModel):
    """One canonical event.

    `metadata` carries everything source-specific, which is what keeps the
    envelope stable while sources differ: a Kubernetes waiting reason and
    a Jenkins failing stage both live there rather than growing the
    envelope a field each.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    event_id: str = Field(default_factory=new_event_id)
    event_type: EventType
    source: EventSource
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    #: The incident this event belongs to, once one exists. Null on an
    #: event observed before intake has resolved it - E4 widens this to a
    #: cross-source correlation key.
    correlation_id: str | None = None
    service: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=32)
    #: Who caused it. "system" for machine-originated events, which is
    #: every Phase 1 event; a human identity once approvals emit events.
    actor: str = Field(default="system", min_length=1, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
