"""Normalized evidence model for the Helix evidence layer.

Everything an agent can learn about the outside world is an `Evidence`
object expressed in Helix vocabulary - never a vendor SDK type (C8). Each
capability (github, jenkins, kubernetes, docker, elk) normalizes its raw
API responses into `Evidence` before returning, and passes the payload
through `redact()` inside its own return path (C6), so nothing unredacted
ever reaches agent context or an LLM prompt.

`Evidence.id` is a stable content hash: hypotheses cite it and the 1.9
provenance record resolves it back to the exact payload that was seen.
"""

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from helix.observability import evidence_collection_counter
from integrations.redaction import redact


class EvidenceSource(StrEnum):
    """The vendor domain a piece of evidence was collected from."""

    GITHUB = "github"
    JENKINS = "jenkins"
    KUBERNETES = "kubernetes"
    DOCKER = "docker"
    ELK = "elk"


class EvidenceKind(StrEnum):
    """What sort of fact an `Evidence` carries. Closed list - capabilities
    must fit their output into one of these rather than leaking vendor
    vocabulary."""

    POD_STATUS = "pod_status"
    POD_LIST = "pod_list"
    EVENT_LIST = "event_list"
    LOG_LINES = "log_lines"
    BUILD_CONSOLE = "build_console"
    TEST_RESULTS = "test_results"
    JOB_INFO = "job_info"
    COMMIT = "commit"
    DIFF = "diff"
    PR_INFO = "pr_info"
    PR_REVIEW = "pr_review"
    CHECK_RUN = "check_run"
    BRANCH_STATE = "branch_state"
    SECRET_INFO = "secret_info"
    IMAGE_INFO = "image_info"
    CONTAINER_STATE = "container_state"


class EvidenceOrigin(StrEnum):
    """Whether the evidence came from a live system or a recorded fixture.
    Provenance must always be able to tell the two apart."""

    LIVE = "live"
    FIXTURE = "fixture"


class EvidenceError(Exception):
    """Base class for every failure an evidence capability can raise."""


class EvidenceAuthError(EvidenceError):
    """Authentication or authorization failed (HTTP 401/403, API token
    rejected, kubeconfig unauthorized)."""


class EvidenceTimeoutError(EvidenceError):
    """The vendor call timed out."""


class EvidenceRateLimitError(EvidenceError):
    """The vendor rate-limited the request (HTTP 429 or equivalent)."""


class EvidenceNotFoundError(EvidenceError):
    """The requested object does not exist (HTTP 404, absent fixture key)."""


class EvidencePartialError(EvidenceError):
    """The vendor answered but the response was structurally incomplete -
    expected sections were absent or the backend reported a partial result,
    so treating it as complete would mislead diagnosis."""


def _evidence_id(source: EvidenceSource, kind: EvidenceKind, payload: dict) -> str:
    """Stable content hash: sha256 over source + kind + the canonical
    (sorted-key) payload, truncated to 16 hex chars and prefixed `ev_`."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(f"{source.value}:{kind.value}:{canonical}".encode()).hexdigest()
    return f"ev_{digest[:16]}"


class Evidence(BaseModel):
    """One normalized fact collected from a vendor system.

    `payload` holds the structured detail (plain JSON types only - no
    vendor objects cross this boundary); `summary` is the one-line human
    form used in prompts and timelines. `truncated`/`total` make partial
    windows explicit (CF-3: a capped result must say it is capped).
    """

    id: str = ""
    source: EvidenceSource
    kind: EvidenceKind
    service: str
    collected_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
    total: int | None = None
    redacted: bool = True
    origin: EvidenceOrigin = EvidenceOrigin.LIVE

    @model_validator(mode="after")
    def _fill_id(self) -> "Evidence":
        if not self.id:
            self.id = _evidence_id(self.source, self.kind, self.payload)
        return self


def build_evidence(
    *,
    source: EvidenceSource,
    kind: EvidenceKind,
    service: str,
    summary: str,
    payload: dict[str, Any],
    origin: EvidenceOrigin,
    truncated: bool = False,
    total: int | None = None,
) -> Evidence:
    """The single funnel every capability returns through.

    Applies `redact()` to the payload and summary here (C6) so no call site
    can forget it, stamps `redacted=True`, and counts the collection so
    observability does not depend on each capability remembering (C4).
    """
    evidence = Evidence(
        source=source,
        kind=kind,
        service=service,
        summary=redact(summary),
        payload=redact(payload),
        truncated=truncated,
        total=total,
        redacted=True,
        origin=origin,
    )
    evidence_collection_counter.add(1, {"source": source.value, "kind": kind.value})
    return evidence
