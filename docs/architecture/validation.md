# Validation Agent (subphase 1.7)

The `validate` node checks the planner's selected candidate **before it
can touch the system**. It is a worker node, not a policy engine: it
verifies the action would make sense if run, records the verdict, and
returns to the coordinator. It never executes the action.

## Verdicts

`state.models.ValidationOutcome` - three values, deliberately:

| Outcome | Meaning | Coordinator does |
|---|---|---|
| `pass` | Every check the family defines ran and passed. | No transition; the run ends at `validating`. |
| `fail` | A check ran and answered "no" - the candidate is wrong. | `validating -> remediation_pending`, plan outputs cleared; the failed fingerprint is excluded from the replan. |
| `not_validatable` | No meaningful pre-check exists, or the check itself could not run. | Same as pass - proceed to the 1.8 policy/execution decision - but recorded distinctly so provenance can tell a checked candidate from an uncheckable one. Never treated as a pass. |

Each verdict carries `reason` (one human-readable line, suitable for
provenance) and `details` (the check-by-check record).

## Dispatch

`ActionSpec.validator` names the strategy; `agent/agents/validation.py`
holds the registry (`validator_for`), which is the only place catalogue
keys map to code - a catalogue action without a wired validator is a
defect the completeness test catches, never a skipped check at runtime.
Strategies live in `agent/validation/`, one module per action family:

| Validator key | Module | Actions | Checks |
|---|---|---|---|
| `kubernetes_target` | `deployments.py` | restart / rollback / scale deployment | Deployment exists in the namespace (evidence lookup); `can_i` for the executor's verb - `patch deployments` for restart/rollback, `update deployments/scale` for scale. |
| `manifest_dry_run` | `manifests.py` | reapply_manifest | Path re-asserted against the k8s allowlist; committed file exists; parses; every doc has apiVersion/kind/metadata.name; `dry_run_apply` accepted. |
| `fix_content` | `fix_pr.py` | open_fix_pr | Per generated file: path re-asserted under `sandbox/payment-api/`; k8s manifests parse + carry required keys + `dry_run_apply`; `*.py` ast.parse; other `*.yaml` safe_load; Dockerfile/others non-empty. |
| `pipeline_exists` | `pipeline.py` | rerun_pipeline | Jenkins `get_job` - job exists (404 is FAIL) and has a previous build (none -> NOT_VALIDATABLE: a never-built job has nothing to re-run, and that cannot be checked honestly). |

Strategies take the catalogue params model (re-validated at the node -
the catalogue bounds are re-asserted, not trusted from state), a
`ValidationContext` (evidence capabilities + `KubernetesPrechecks` +
repo root), and return `ValidationResult`. They see only Helix
vocabulary - no kubernetes/jenkins SDK types (C8).

Check failures vs check errors: a check that *answered no* is FAIL; a
check that *could not run* (no kubeconfig, timeout, the auth query
itself denied) raises an `EvidenceError` the strategy maps to
NOT_VALIDATABLE. "Could not check" is never a pass and never a wrong
FAIL.

## Prechecks (`integrations/kubernetes/prechecks.py`)

Two read-side operations behind the `KubernetesPrechecks` capability
protocol, implemented over the **python kubernetes client** (not a
kubectl runner - the same dependency the evidence layer already uses):

- `can_i(verb, resource, namespace, group)` - a `SelfSubjectAccessReview`
  via `AuthorizationV1Api` (`kubectl auth can-i` semantics). `resource`
  accepts a subresource as `"deployments/scale"`.
- `dry_run_apply(manifest_text)` - `utils.create_from_yaml(...,
  apply=True, dry_run="All")`: a server-side apply with `dryRun=All`, so
  schema validation and admission webhooks run but nothing is persisted
  (`kubectl apply --dry-run=server` semantics). Rejections come back as
  `FailToCreateError`/`DynamicApiError` and surface as `ok=False` with
  the server's message.

Both return `PrecheckOutcome(ok, detail, checks)` when the cluster
answered; a check that could not run raises the usual EvidenceError
family. The fixture impl (`FixtureKubernetesPrechecks`) returns canned
`ok=True` outcomes so fixture-mode runs exercise the dispatch - the
recorded `detail` says "fixture: canned pass" so provenance can tell it
apart. Injected seams (`authz_v1`, `apply_fn`) let unit tests exercise
the plumbing without a cluster; genuinely live calls are
integration-marked.

The rerun_pipeline check uses `JenkinsEvidence.get_job` (`job_info`
evidence kind, 1.7): `GET /job/<job>/api/json` normalized to `{job, url,
buildable, last_build, last_completed_build}` - one read answers both
"job exists" and "has built before".

## Persistence and the exclusion list

Every validate pass appends a `validations` row: incident FK, candidate
FK (nullable), `attempt`, action, params (JSONB), `fingerprint`, outcome,
reason, details, `created_at`.

`fingerprint = sha256(action | canonical(params))` is the candidate's
identity. On FAIL the node appends it to `WorkflowState
excluded_candidates`; the `fail` validations rows ARE the persisted
exclusion list - intake rebuilds the in-state list from them on every
fresh run, so a once-failed candidate can never be re-selected, across
the in-run replan and across runs. The planner consults the list inside
`rank_candidates` (a re-proposed excluded fingerprint is dropped with a
recorded reason, before scoring).

## Retry hook

`validation_attempts` counts validate passes per run. First FAIL ->
replan (once). Second consecutive FAIL -> `validating -> escalated` -
the single-retry hook is exhausted; the real retry-budget machinery is
1.10. PASS/NOT_VALIDATABLE leave `validating` standing as the resumable
non-terminal status the 1.8 policy/execution node picks up from.
