"""`fix_content` validator (subphase 1.7): open_fix_pr.

The PR's generated file contents are checked per file, before any PR
opens - a PR that would land a broken file is a wrong candidate:

- k8s manifests (under sandbox/payment-api/k8s/, *.yaml): parse,
  apiVersion/kind/metadata.name present, then `dry_run_apply` - the same
  admission check a direct reapply would face.
- *.py: `ast.parse` - syntax must be plausible.
- other *.yaml/*.yml: `yaml.safe_load` must parse.
- Dockerfile: non-empty content.
- anything else: non-empty content.

Every path must stay under the sandbox/payment-api/ write root -
enforced at param expression and re-asserted here, since the validator
is the last check before the 1.8 GitHub write path sees the files.
"""

import ast

import yaml
from pydantic import BaseModel

from agent.actions.catalogue import REPO_WRITE_ROOT, ActionSpec
from agent.validation import ValidationContext, ValidationResult
from agent.validation.manifests import _required_keys_ok
from integrations.evidence import EvidenceError
from state.models import ValidationOutcome

_K8S_DIR = f"{REPO_WRITE_ROOT}k8s/"


def _is_k8s_manifest(path: str) -> bool:
    return path.startswith(_K8S_DIR) and path.endswith((".yaml", ".yml"))


def _check_manifest(path: str, content: str, ctx: ValidationContext) -> str | None:
    """None when the manifest parses, carries the required keys and passes
    the server dry-run; else the failure reason."""
    try:
        docs = [d for d in yaml.safe_load_all(content) if d is not None]
    except yaml.YAMLError as exc:
        return f"{path}: manifest does not parse: {exc}"
    if not docs:
        return f"{path}: manifest contains no objects"
    if reason := _required_keys_ok(docs):
        return f"{path}: {reason}"
    # A raised EvidenceError propagates - the check itself failing is
    # NOT_VALIDATABLE (handled by the caller), never a wrong FAIL.
    outcome = ctx.prechecks.dry_run_apply(content)
    return None if outcome.ok else f"{path}: server dry-run rejected it: {outcome.detail}"


def _check_file(path: str, content: str, ctx: ValidationContext) -> str | None:
    """Per-file check dispatch. Returns None when the file is plausible,
    else the failure reason."""
    if not path.startswith(REPO_WRITE_ROOT):
        return f"{path}: outside the {REPO_WRITE_ROOT} write root"
    if _is_k8s_manifest(path):
        return _check_manifest(path, content, ctx)
    name = path.rsplit("/", 1)[-1]
    if name.endswith(".py"):
        try:
            ast.parse(content, filename=path)
        except SyntaxError as exc:
            return f"{path}: python does not parse: {exc}"
        return None
    if name.endswith((".yaml", ".yml")):
        try:
            if yaml.safe_load(content) is None:
                return f"{path}: yaml parses to an empty document"
        except yaml.YAMLError as exc:
            return f"{path}: yaml does not parse: {exc}"
        return None
    if name == "Dockerfile" or name.endswith(".dockerfile"):
        return None if content.strip() else f"{path}: empty Dockerfile"
    return None if content.strip() else f"{path}: empty file content"


def validate_fix_content(
    spec: ActionSpec, params: BaseModel, ctx: ValidationContext
) -> ValidationResult:
    failures = []
    checked = []
    not_validatable = []
    for file_change in params.files:
        try:
            reason = _check_file(file_change.path, file_change.content, ctx)
        except EvidenceError as exc:
            # The dry-run check could not run - honest NOT_VALIDATABLE
            # rather than a hollow pass or a wrong FAIL.
            not_validatable.append(f"{file_change.path}: {exc}")
            continue
        checked.append(file_change.path)
        if reason:
            failures.append(reason)
    if failures:
        return ValidationResult(
            outcome=ValidationOutcome.FAIL,
            reason="; ".join(failures),
            details={"checked": checked, "not_validatable": not_validatable},
        )
    if not_validatable:
        return ValidationResult(
            outcome=ValidationOutcome.NOT_VALIDATABLE,
            reason="dry-run precheck could not run: " + "; ".join(not_validatable),
            details={"checked": checked},
        )
    return ValidationResult(
        outcome=ValidationOutcome.PASS,
        reason=f"all {len(checked)} file(s) pass content checks",
        details={"checked": checked},
    )
