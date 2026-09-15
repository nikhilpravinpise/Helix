"""Validation strategies for the Validation Agent (subphase 1.7).

Every catalogue action declares a `validator` key; this package holds the
strategy each key resolves to (wired in `agent/agents/validation.py`).
The contract every strategy honors:

- Return `ValidationResult` - PASS (checked, looks right), FAIL (checked,
  is wrong) or NOT_VALIDATABLE (no meaningful pre-check exists / the
  check itself could not run). NOT_VALIDATABLE always carries a reason
  and is NEVER a pass: it means "proceed to the 1.8 policy/execution
  decision unchecked" and is recorded distinctly so provenance can tell
  a validated candidate from an uncheckable one.
- NEVER execute the action. Strategies may only read: evidence
  capabilities, `can_i` auth queries, and server-side dry-run applies
  (integrations/kubernetes/prechecks.py - dryRun=All persists nothing).
- No vendor types: strategies see capability Protocols,
  `PrecheckOutcome`, and `Evidence` - never a V1Pod or a Jenkins build
  object (C8).

`candidate_fingerprint` is the candidate's identity for repeat-action
prevention: the validate node records it on every `validations` row, and
the planner drops proposals whose fingerprint appears in the exclusion
list rebuilt from prior FAIL rows.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from integrations.capabilities import JenkinsEvidence, KubernetesEvidence, KubernetesPrechecks
from state.models import ValidationOutcome

__all__ = [
    "ValidationContext",
    "ValidationOutcome",
    "ValidationResult",
    "candidate_fingerprint",
]


class ValidationResult(BaseModel):
    """The verdict one strategy pass produced. `details` holds the
    check-by-check record the validations row persists."""

    outcome: ValidationOutcome
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ValidationContext:
    """What a strategy may consult, in Helix vocabulary only: the
    kubernetes/jenkins evidence capabilities, the kubernetes prechecks
    (can_i / dry_run_apply), and the repo root for reading committed
    manifests."""

    kubernetes: KubernetesEvidence
    jenkins: JenkinsEvidence
    prechecks: KubernetesPrechecks
    repo_root: Path


def candidate_fingerprint(action: str, params: dict[str, Any]) -> str:
    """sha256 over `action | canonical(params)` - the stable identity of a
    proposed (action, params) pair. Params arrive already normalized
    (params_model.model_dump), so equivalent re-proposals hash equal."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{action}|{canonical}".encode()).hexdigest()
