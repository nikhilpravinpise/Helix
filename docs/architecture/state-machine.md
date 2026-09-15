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
| `recovered` | Terminal: verification confirmed the desired state. |
| `escalated` | Terminal: Helix gave up and handed the incident to a human. |
| `failed` | Terminal for the run: the workflow itself failed. Re-enterable via `failed -> diagnosing` (rediagnosis). |

## Allowed edges

Enforced by `machine.transition()` - anything not listed raises
`InvalidTransitionError`. The table below is the composition of the
workflow edges plus the blanket rule that **any non-terminal status can go
to `escalated` or `failed`** (an investigation can be abandoned or give up
from anywhere).

| From | To |
|---|---|
| `received` | `diagnosing` |
| `diagnosing` | `remediation_pending` |
| `remediation_pending` | `validating` |
| `validating` | `awaiting_approval`, `executing` |
| `awaiting_approval` | `executing`, `remediation_pending` (rejected -> replan) |
| `executing` | `verifying`, `diagnosing` (execution evidence contradicts the hypothesis -> rediagnose), `failed` |
| `verifying` | `recovered`, `remediation_pending` (failed verification -> replan), `escalated` |
| `failed` | `diagnosing` (rediagnosis after a failed run) |
| any non-terminal | `escalated`, `failed` |
| `recovered`, `escalated` | (none - terminal) |

Every transition writes a `transitions` row (`incident_id`, `from_status`,
`to_status`, `cause`, `created_at`) and updates `incidents.status` in the
same transaction, so the persisted timeline cannot disagree with the
status. Incident creation writes the timeline's first row as
`NULL -> received` (`machine.record_creation`).

## Scope: what CONTEXT.md section 14 does NOT mean here

Section 14's example lifecycle (`TRIAGED -> OWNER_ASSIGNED ->
REVIEW_PENDING -> REJECTED -> FIX_REQUIRED -> APPROVED -> DEPLOYING ->
FAILED -> DIAGNOSING -> ...`) is the **Phase 2** review-driven flow.
`TRIAGED`, `OWNER_ASSIGNED`, `REVIEW_PENDING`, `REJECTED`, `FIX_REQUIRED`,
`APPROVED` and `DEPLOYING` are deliberately absent from this enum - they
arrive with the Phase 2 triage/review agents.

The example's `EXECUTING -> VALIDATING -> RECOVERED` tail refers to
post-deploy validation inside that Phase 2 lifecycle. Phase 1 validates
pre-execution (plan.md 1.7) and verifies post-execution (1.9), hence the
distinct `VERIFYING` status here rather than reusing `VALIDATING`.

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
