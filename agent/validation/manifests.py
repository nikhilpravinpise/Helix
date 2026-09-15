"""`manifest_dry_run` validator (subphase 1.7): reapply_manifest.

Checks, in order:

1. The path is in the committed-manifest allowlist - enforced at param
   expression by `ReapplyManifestParams` and re-asserted here so the
   validator alone can never be talked around it.
2. The committed file exists in the repo and parses as YAML documents,
   each a mapping carrying apiVersion + kind + metadata.name.
3. `dry_run_apply` - server-side apply with dryRun=All: schema validation
   and admission run, nothing persists. A rejection is FAIL; the check
   not running at all is NOT_VALIDATABLE.
"""

import yaml
from pydantic import BaseModel

from agent.actions.catalogue import MANIFEST_ALLOWLIST, ActionSpec
from agent.validation import ValidationContext, ValidationResult
from integrations.evidence import EvidenceError
from state.models import ValidationOutcome


def _required_keys_ok(docs: list) -> str | None:
    """None when every doc is a k8s-shaped mapping; else a reason."""
    for doc in docs:
        if not isinstance(doc, dict):
            return "manifest contains a non-object document"
        missing = [key for key in ("apiVersion", "kind") if not doc.get(key)]
        metadata = doc.get("metadata")
        if not isinstance(metadata, dict) or not metadata.get("name"):
            missing.append("metadata.name")
        if missing:
            kind = doc.get("kind") or doc.get("apiVersion") or "document"
            return f"{kind} missing required keys: {', '.join(missing)}"
    return None


def validate_manifest_dry_run(
    spec: ActionSpec, params: BaseModel, ctx: ValidationContext
) -> ValidationResult:
    path = params.path
    if path not in MANIFEST_ALLOWLIST:
        return ValidationResult(
            outcome=ValidationOutcome.FAIL,
            reason=f"manifest path {path} is not in the committed k8s allowlist",
        )
    manifest_file = ctx.repo_root / path
    if not manifest_file.is_file():
        return ValidationResult(
            outcome=ValidationOutcome.FAIL,
            reason=f"committed manifest {path} does not exist in the repo",
        )
    text = manifest_file.read_text(encoding="utf-8")
    try:
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError as exc:
        return ValidationResult(
            outcome=ValidationOutcome.FAIL,
            reason=f"manifest {path} does not parse: {exc}",
        )
    if not docs:
        return ValidationResult(
            outcome=ValidationOutcome.FAIL,
            reason=f"manifest {path} contains no objects",
        )
    if reason := _required_keys_ok(docs):
        return ValidationResult(outcome=ValidationOutcome.FAIL, reason=reason)

    try:
        outcome = ctx.prechecks.dry_run_apply(text)
    except EvidenceError as exc:
        return ValidationResult(
            outcome=ValidationOutcome.NOT_VALIDATABLE,
            reason=f"server dry-run could not run: {exc}",
        )
    if not outcome.ok:
        return ValidationResult(
            outcome=ValidationOutcome.FAIL,
            reason=f"server dry-run rejected {path}: {outcome.detail}",
            details={"dry_run": outcome.checks},
        )
    return ValidationResult(
        outcome=ValidationOutcome.PASS,
        reason=f"{path}: {outcome.detail}",
        details={"dry_run": outcome.checks},
    )
