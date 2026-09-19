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

- **`service.unhealthy` - accepted.** The captured `config-mismatch`
  fault holds a pod that is `phase: Running` with its container running
  and its `Ready` condition `False`: up, and not serving. Nothing died and
  nothing about that pod is a deployment, so `pod.failed` and
  `deployment.failed` would both be false. No Phase 1 *signal* produces it
  - it has no rule in `events.classify` - but the E3 Kubernetes adapter
  emits it from exactly this pod shape.

  E1 originally justified this with `dependency-failure` instead, on the
  reasoning that scaling its downstream to zero would fail readiness.
  Checking the fixture in E3 showed that pod is `Ready: True` - the
  sandbox deployment declares no readiness probe, so `/ready` returning
  503 to a caller never reaches the pod's conditions. The decision stands
  and its evidence is now a pod that actually exhibits the state.

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

import hashlib
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

    Random, for events with no vendor payload behind them to derive an
    identity from - Helix's own lifecycle events, and the Phase 1 signal
    path. Anything built from a vendor payload uses `derive_event_id`
    instead.
    """
    return f"evt_{secrets.token_hex(6)}"


def derive_event_id(source: str, *parts: object) -> str:
    """A stable id for an event built from a vendor payload (E3).

    The E3 idempotency requirement is that a redelivered vendor webhook
    yields the same event id, so the id has to be a function of the
    payload's own identity rather than of when it arrived. `parts` is that
    natural key - a Kubernetes object uid, a Jenkins job and build number,
    a GitHub review id - and it must contain only fields the vendor
    repeats verbatim on redelivery. Anything that varies between
    deliveries (a receive timestamp, a queue offset) would defeat the
    whole mechanism.

    Truncated to 12 hex characters to match `new_event_id`'s shape. That
    is 48 bits, which is ample for a deduplication window and is not
    offered as a cryptographic guarantee.
    """
    material = "|".join([str(source), *(str(p) for p in parts)])
    return f"evt_{hashlib.sha256(material.encode()).hexdigest()[:12]}"


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
