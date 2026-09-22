# Incident state machine and intake (subphase 1.4)

The incident is the canonical active-state record (CONTEXT.md section 13).
It lives in the host Postgres (`infra/postgres-compose.yml`, decision D3)
and is mutated only through `state/machine.py` and `state/intake.py`.

## Statuses

`state.machine.IncidentStatus` - serialized as the lowercase values shown
(`"received"`, matching the section 13 example shape):

| Status | Meaning |
|---|---|
| `received` | A signal created the incident; nothing has run yet. |
| `diagnosing` | The Diagnosis Agent is collecting evidence and forming a hypothesis (1.5). |
| `remediation_pending` | A hypothesis exists; planning produced candidates (1.6). |
| `validating` | The selected candidate is being checked before it can touch the system (1.7). |
| `awaiting_approval` | Policy requires a human decision; the workflow is suspended (1.8). |
| `executing` | An approved/allowed action is being applied (1.8). |
| `verifying` | Post-action desired-state checks are running (1.9). |
| `recovered` | Terminal: verification confirmed the desired state (both lifecycles converge here). |
| `escalated` | Terminal: Helix gave up and handed the incident to a human. |
| `failed` | Terminal for the run: the workflow itself failed. Re-enterable via `failed -> diagnosing` (rediagnosis). |
| `triaged` | (Phase 2, `kind=change` only) A PR/push event was triaged to an affected service and blast radius (2.5). |
| `owner_assigned` | (Phase 2, `kind=change` only) Ownership resolved from graph/codeowners/registry/commit history (2.5). |
| `review_pending` | (Phase 2, `kind=change` only) A reviewer was selected and asked via Slack; decision awaited (2.6). |
| `rejected` | (Phase 2, `kind=change` only) The reviewer rejected; a fix is required. |
| `fix_required` | (Phase 2, `kind=change` only) Rejected change awaiting the author's fix; fix returns to `review_pending`. |
| `approved` | (Phase 2, `kind=change` only) The reviewer approved; the change may deploy. |
| `deploying` | (Phase 2, `kind=change` only) Approved change deploying; clean deploy -> `recovered`, failed deploy -> `diagnosing` (Ops handoff). |

## Allowed edges

Enforced by `machine.transition()` - anything not listed raises
`InvalidTransitionError`. The table below is the composition of the
workflow edges plus the blanket rule that **any non-terminal status can go
to `escalated` or `failed`** (an investigation can be abandoned or give up
from anywhere).

| From | To |
|---|---|
| `received` | `diagnosing`, `triaged` (`kind=change` enters the review lifecycle, 2.1) |
| `diagnosing` | `remediation_pending` |
| `remediation_pending` | `validating` |
| `validating` | `awaiting_approval`, `executing`, `remediation_pending` (failed validation -> replan) |
| `awaiting_approval` | `executing`, `remediation_pending` (rejected -> replan) |
| `executing` | `verifying`, `diagnosing` (execution evidence contradicts the hypothesis -> rediagnose), `remediation_pending` (refused execution replans in run, 2.1), `failed` |
| `verifying` | `recovered`, `remediation_pending` (failed verification -> replan), `escalated` |
| `failed` | `diagnosing` (rediagnosis after a failed run), `escalated` (retry budget exhausted, 1.10) |
| `triaged` | `owner_assigned`, plus `escalated`/`failed` (missing owner escalates, 2.5) |
| `owner_assigned` | `review_pending`, plus `escalated`/`failed` |
| `review_pending` | `approved`, `rejected`, plus `escalated`/`failed` |
| `rejected` | `fix_required`, plus `escalated`/`failed` |
| `fix_required` | `review_pending` (fix returns to review, never straight to `approved`), plus `escalated`/`failed` |
| `approved` | `deploying`, plus `escalated`/`failed` |
| `deploying` | `recovered` (clean deploy), `diagnosing` (failed deploy hands off to the Ops recovery loop, 2.9), plus `escalated`/`failed` |
| any non-terminal | `escalated`, `failed` |
| `recovered`, `escalated` | (none - terminal) |

Every transition writes a `transitions` row (`incident_id`, `from_status`,
`to_status`, `cause`, `created_at`) and updates `incidents.status` in the
same transaction, so the persisted timeline cannot disagree with the
status. Incident creation writes the timeline's first row as
`NULL -> received` (`machine.record_creation`).

The 1.8 edges carry the safety gate: `validating -> awaiting_approval`
parks a `pending` approvals row in the same transaction (the gate can
never exist without a decision to make), `awaiting_approval ->
executing` fires only once that row is `approved`, and `awaiting_approval
-> remediation_pending` replans with the rejected fingerprint excluded.
`executing -> verifying`/`failed` come from the persisted `executions`
row's result. The full gate semantics - verdict table, durable
suspension, the executor's second check - live in
[safety-policy.md](safety-policy.md); the 1.10 bounds (compensation
debt, retry budget, approval/step timeouts) live in
[unhappy-paths.md](unhappy-paths.md).

## Scope: what CONTEXT.md section 14 means here

Section 14's example lifecycle (`TRIAGED -> OWNER_ASSIGNED ->
REVIEW_PENDING -> REJECTED -> FIX_REQUIRED -> APPROVED -> DEPLOYING ->
FAILED -> DIAGNOSING -> ...`) is the **Phase 2** review-driven flow for
`Incident.kind=change` (PR/push events, 2.1/2.9). `recovery` incidents
(a failure signal) use the Phase 1 chain unchanged; `change` incidents
use the Phase 2 chain above. The two chains meet where CONTEXT.md
section 16 says they must: a failed deploy (`DEPLOYING -> DIAGNOSING`)
re-enters the Phase 1 recovery loop through the same `DIAGNOSING` a
recovery incident uses, and a clean deploy closes the change incident
as `RECOVERED` - one terminal vocabulary for both lifecycles.

## Two different "environments"

`Signal.environment` / `incidents.environment` is the incident's **target**
environment - `sandbox` | `staging` | `production`. The policy engine
(1.8) gates on it: production mutations need approval.

`HELIX_ENVIRONMENT` is Helix's **own** runtime mode (`development`, ...),
which configures Helix's logging and behavior. A signal saying
`environment=production` describes where the failure lives, never how
Helix itself is running. The two must never be conflated.

## Intake and deduplication

Push model (D6): emitters POST a `state.intake.Signal` to
`POST /intake/kubernetes` or `POST /intake/jenkins`
(`scripts/emit-signal.sh`, the Jenkinsfile `post { failure }` block), or
feed the same JSON to `helix intake --file`. Both paths call
`intake_signal()`.

Fingerprint: `sha256(source | service | environment | canonical(signature))`
where canonical means sorted-key compact JSON. `signature` is the
fault-identifying subset of the payload:

| Source | Signature keys |
|---|---|
| kubernetes | `waiting_reason`, `container` |
| jenkins | `job`, `stage` |
| github | (whole signature - no documented subset yet) |

Only the documented keys feed the fingerprint, so volatile fields
elsewhere in `signature` (pod-name suffixes, build numbers) cannot split
one failure into multiple incidents. If a signal carries none of the
documented keys, the whole signature is used - deterministic, and biased
toward separate incidents over wrong merges.

A signal matching an **open** (non-terminal) incident's fingerprint
attaches as another `signals` row. A signal matching only a terminal
incident's fingerprint creates a **new** incident - a recovered or failed
failure surfacing again is a new occurrence. This is what makes "the same
fault three times in quick succession" produce one incident with three
signals (the 1.4 exit criterion).

### Known limitations

- **Same-source dedup only.** One underlying failure surfacing as both a
  Jenkins and a Kubernetes signal produces two incidents. Cross-source
  correlation is Phase 1.5 (E4). Phase 1 benchmark scenarios emit one
  signal each, so scoring is not distorted.
- **Concurrent duplicate signals** on different sessions can race past the
  open-incident lookup and create two incidents. The single-active-run
  guard (`workflow_runs`, D13) landed in basic form in 1.5 - one `running`
  run per incident, enforced by a partial unique index; stale-run
  reclamation and bounded concurrency are 1.10 hardening.

## Auth and ports

`helix serve` binds `HELIX_API_BIND:HELIX_API_PORT` (default
127.0.0.1:8787 - chosen to avoid payment-api 8000, Jenkins 8080,
Elasticsearch 9200). When `HELIX_INTAKE_TOKEN` is set, intake POSTs
require `Authorization: Bearer <token>`; unset means local-only trust,
matching the rest of the sandbox's posture.
