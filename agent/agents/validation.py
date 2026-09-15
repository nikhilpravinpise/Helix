"""Validation Agent node (subphase 1.7): check the selected candidate
before it can touch the system.

Flow per run of the node:

1. Read `selected_action` (the rank-1 candidate dict the planner wrote).
   Params are re-validated against the catalogue params model - the state
   dict could in principle carry anything, so the catalogue's bounds are
   re-asserted rather than trusted.
2. Dispatch on the ActionSpec's `validator` key to a strategy in
   `agent/validation/` - one per action family (kubernetes targets,
   manifest dry-run, fix-PR content, pipeline existence). Strategies are
   strictly read-side: evidence lookups, `can_i` auth queries, and
   server-side dry-run applies. Validation NEVER executes the action.
3. Persist a `validations` row (incident, candidate, action, params,
   fingerprint, outcome, reason, details, attempt) and write the result
   into `state["validation"]`.
4. On FAIL the candidate's fingerprint joins `excluded_candidates` -
   persisted via the validations table, so the planner can never
   re-propose it, across the replan and across runs.

The coordinator owns what happens next: FAIL -> remediation_pending for a
single replan (a second consecutive failure escalates); PASS and
NOT_VALIDATABLE leave the incident at `validating`, where the run ends -
`validating` is a resumable non-terminal status and the 1.8
policy/execution node picks the run up from here.
"""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from agent.actions.catalogue import ActionSpec, get_action
from agent.state import WorkflowState
from agent.validation import (
    ValidationContext,
    ValidationResult,
    candidate_fingerprint,
)
from agent.validation.deployments import validate_kubernetes_target
from agent.validation.fix_pr import validate_fix_content
from agent.validation.manifests import validate_manifest_dry_run
from agent.validation.pipeline import validate_pipeline_exists
from helix.observability import trace_workflow_step
from state import models
from state.models import ValidationOutcome

if TYPE_CHECKING:
    from agent.graph import GraphDeps

#: Repo root for strategies that read committed files (reapply_manifest).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Catalogue `validator` key -> strategy. The registry completeness test
#: asserts every registered action's key resolves here - a catalogue
#: action without a wired validator is a defect, not a runtime condition.
ValidatorFn = Callable[[ActionSpec, BaseModel, ValidationContext], ValidationResult]

_VALIDATORS: dict[str, ValidatorFn] = {
    "kubernetes_target": validate_kubernetes_target,
    "manifest_dry_run": validate_manifest_dry_run,
    "fix_content": validate_fix_content,
    "pipeline_exists": validate_pipeline_exists,
}


def validator_for(spec: ActionSpec) -> ValidatorFn:
    """Resolve the spec's `validator` key; an unregistered key is a
    catalogue defect and raises loudly rather than skipping the check."""
    strategy = _VALIDATORS.get(spec.validator)
    if strategy is None:
        raise ValueError(
            f"action {spec.name!r} names validator {spec.validator!r} "
            f"which has no registered strategy"
        )
    return strategy


def _latest_selected_candidate_id(session, incident_pk: int) -> int | None:
    row = session.scalar(
        select(models.Candidate.id)
        .where(models.Candidate.incident_id == incident_pk)
        .where(models.Candidate.selected.is_(True))
        .order_by(models.Candidate.id.desc())
        .limit(1)
    )
    return row


def _persist(
    deps: "GraphDeps",
    state: WorkflowState,
    action: str,
    params: dict,
    fingerprint: str,
    result: ValidationResult,
) -> None:
    """Append the verdict to `validations` - the provenance record of what
    was checked, and (on fail) the persisted exclusion the replan and any
    later run both consult."""
    with deps.session_scope() as session:
        session.add(
            models.Validation(
                incident_id=state["incident_pk"],
                candidate_id=_latest_selected_candidate_id(session, state["incident_pk"]),
                attempt=state.get("attempts", 0),
                action=action,
                params=params,
                fingerprint=fingerprint,
                outcome=result.outcome,
                reason=result.reason,
                details=result.details,
            )
        )


def _run_strategy(
    selected: dict, ctx: ValidationContext
) -> tuple[ValidationResult, str, str, dict]:
    """Dispatch the selected candidate to its validator. Returns
    (result, action_name, fingerprint, params_dict). Params failing
    revalidation fail the candidate outright - the catalogue's bounds are
    the first safety gate and stay the first gate here."""
    action = str(selected.get("action") or "")
    raw_params = selected.get("params") or {}
    spec = get_action(action) if action else None
    if spec is None:
        return (
            ValidationResult(
                outcome=ValidationOutcome.FAIL,
                reason=(
                    "no selected candidate to validate"
                    if not action
                    else f"action {action!r} is not in the catalogue"
                ),
            ),
            action or "(none)",
            "",
            raw_params,
        )
    try:
        params = spec.params_model.model_validate(raw_params)
    except ValidationError as exc:
        return (
            ValidationResult(
                outcome=ValidationOutcome.FAIL,
                reason=f"candidate params failed validation: {exc.errors()[0]['msg']}",
            ),
            spec.name,
            candidate_fingerprint(spec.name, raw_params),
            raw_params,
        )
    normalized = params.model_dump(mode="json")
    return (
        validator_for(spec)(spec, params, ctx),
        spec.name,
        candidate_fingerprint(spec.name, normalized),
        normalized,
    )


def validation_node(deps: "GraphDeps"):
    """The validate graph node: dispatch -> persist -> state. Wrapped in
    trace_workflow_step like every node (C4)."""

    def _validate(state: WorkflowState) -> dict:
        with trace_workflow_step("validate"):
            errors: list[str] = list(state.get("errors", []))
            attempts = state.get("validation_attempts", 0) + 1
            ctx = ValidationContext(
                kubernetes=deps.kubernetes,
                jenkins=deps.jenkins,
                prechecks=deps.kube_prechecks,
                repo_root=REPO_ROOT,
            )

            selected = state.get("selected_action") or {}
            result, action, fingerprint, params = _run_strategy(selected, ctx)
            _persist(deps, state, action, params, fingerprint, result)

            excluded = list(state.get("excluded_candidates") or [])
            if (
                result.outcome == ValidationOutcome.FAIL
                and fingerprint
                and fingerprint not in excluded
            ):
                excluded.append(fingerprint)

            return {
                "validation": {
                    "action": action,
                    "params": params,
                    "outcome": result.outcome.value,
                    "reason": result.reason,
                    "details": result.details,
                },
                "validation_attempts": attempts,
                "excluded_candidates": excluded,
                "errors": errors,
            }

    return _validate
