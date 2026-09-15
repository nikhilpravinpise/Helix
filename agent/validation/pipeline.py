"""`pipeline_exists` validator (subphase 1.7): rerun_pipeline.

A re-run needs a job that exists and has built before - `get_job` answers
both in one read. The honest outcomes:

- job 404 -> FAIL: re-running a job that does not exist cannot succeed.
- job exists but has never built -> NOT_VALIDATABLE: there is no prior
  run to re-run, and whether triggering a fresh first build remediates
  anything cannot be checked - it proceeds to the policy/execution
  decision flagged as unchecked, never counted as a pass.
- the lookup itself errors -> NOT_VALIDATABLE: "could not check" is not
  a verdict.
"""

from pydantic import BaseModel

from agent.actions.catalogue import ActionSpec
from agent.validation import ValidationContext, ValidationResult
from integrations.evidence import EvidenceError, EvidenceNotFoundError
from state.models import ValidationOutcome


def validate_pipeline_exists(
    spec: ActionSpec, params: BaseModel, ctx: ValidationContext
) -> ValidationResult:
    job = params.job
    try:
        evidence = ctx.jenkins.get_job(job)
    except EvidenceNotFoundError:
        return ValidationResult(
            outcome=ValidationOutcome.FAIL,
            reason=f"jenkins job {job} does not exist",
        )
    except EvidenceError as exc:
        return ValidationResult(
            outcome=ValidationOutcome.NOT_VALIDATABLE,
            reason=f"could not check jenkins job {job}: {exc}",
        )
    last_build = evidence.payload.get("last_build")
    if last_build is None:
        return ValidationResult(
            outcome=ValidationOutcome.NOT_VALIDATABLE,
            reason=f"jenkins job {job} exists but has no previous build to re-run",
        )
    return ValidationResult(
        outcome=ValidationOutcome.PASS,
        reason=f"jenkins job {job} exists; last build #{last_build}",
        details={"job": job, "last_build": last_build},
    )
