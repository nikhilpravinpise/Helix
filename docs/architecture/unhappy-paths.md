# Unhappy paths and bounded recovery (subphase 1.10)

The happy-path loop (diagnose -> plan -> validate -> policy/execute ->
verify -> recovered) is now bounded on every failure edge. This document
covers the five mechanisms, in the order a failure meets them.

```text
verify FAIL
  |-- action is IRREVERSIBLE        -> ESCALATED (nothing can be undone)
  |-- action is COMPENSABLE         -> `compensate` node runs the undo
  |     |-- undo SUCCEEDED          -> consume attempt -> replan
  |     `-- undo refused/errored    -> ESCALATED (un-undone change needs a human)
  `-- otherwise (stateless/paid)    -> consume attempt -> replan

replan / rediagnosis consumes one attempt per cycle; when
incidents.attempt_count reaches HELIX_MAX_ATTEMPTS the incident
ESCALATES as "retry budget exhausted" instead of looping.
```

## Compensation for failed verification

A `compensable` action whose verification failed left a persistent
change the world did not want - verification cannot establish desired
state, so the change must be undone before Helix tries anything else.
The durable debt is `verifications.needs_compensation`; "unpaid" means no
`success` executions row compensating the failed execution.

The `compensate` node (`agent/agents/compensation.py`):

1. Finds the newest unpaid debt (`_unpaid_debt`), derives the undo from
   the catalogue's `compensating_action` plus the failed execution's
   recorded `params`/`output`.
2. Dispatches it:
   - catalogue compensations (`scale_deployment` back to
     `output["previous_replicas"]`, `rollback_deployment` again,
     `reapply_manifest` of the same file) run through the same
     `enforce_execution_gate` + executor path as any write - a
     compensating write is still a write;
   - executor-side ops the planner can never propose (`close_pr` for
     `open_fix_pr`) dispatch directly, under the failed execution's own
     authorization record.
3. Persists every attempt as an `executions` row with
   `compensates_id` -> the verification-failed row. Only `success` pays
   the debt; `refused`/`error` rows are audit, not payment.
4. Hands back to the coordinator: success -> bounded replan;
   refused/error -> ESCALATED. A compensation is never retried inside
   the node - one shot per failed execution is the bound.

Params are derived, never guessed: a `scale_deployment` row without
`output["previous_replicas"]` (the executor captures the pre-write count
from deployment status at dispatch time) is refused and escalates.

## Bounded retry budget

`incidents.attempt_count` is the per-incident ledger. One attempt is
consumed at each conclusion of a failed cycle:

- `verifying` fail -> replan or escalate (one attempt),
- `failed` -> `diagnosing` re-entry or escalate (one attempt),
- `verifying`/`failed` -> `escalated` when `attempt_count + 1 >=
  HELIX_MAX_ATTEMPTS` (cause: "retry budget exhausted").

The counter increments inside the coordinator's transition transaction -
the same commit that writes the `transitions` row - so a crash cannot
lose a consumed attempt. Default budget is 3 (`HELIX_MAX_ATTEMPTS`).

The older single-validation-retry rule (`validating` FAIL replans once,
a second consecutive FAIL escalates) is unchanged - it is a
within-the-cycle bound, not part of the attempt ledger.

## Execution failure -> rediagnosis (in-run replan for refusals, 2.1)

A `refused` executions row (the executor's own gate said no before any
transport call - a candidate-level refusal) transitions `executing ->
remediation_pending` and replans in run for arms with a planner (E, A),
bounded by the exclusion list as the budget. An `error` result (transport
failure) still transitions `executing -> failed` with the fingerprint
excluded; a later `helix run` on a `failed` incident takes `failed ->
diagnosing` (budget permitting): hypothesis and
run-scoped outputs are cleared, `attempt_count` bumps, and the planner's
exclusion list keeps the failed fingerprint from being re-selected.

## Approval timeout

An incident suspended at `awaiting_approval` forever is a hang, so a
pending `approvals` row older than `HELIX_APPROVAL_TIMEOUT` (default
24h, `0` disables) is handled two ways:

- **The sweep** (`state/approvals.sweep_approval_timeout`) runs inside
  `run_incident` while holding the incident row lock, before any resume:
  the row is recorded `rejected` (`decided_by="helix"`, the note carries
  the pending age) and the incident transitions
  `awaiting_approval -> escalated` with the timeout cause - all in the
  run's first transaction.
- **The decision-side guard**: `record_decision` refuses a decision on a
  pending row that has already outlived the window
  (`ApprovalDecidedError` -> CLI error / API 409). It writes nothing -
  the sweep owns marking the row; the guarantee is that a timed-out
  approval can never execute as if still pending.

There is no background reaper in Phase 1: the sweep is evaluated when a
run is driven (`helix run`, the intake `?run=true` hook, an
approve/reject resume). An incident nobody ever drives sits at
`awaiting_approval` - the stored row is honest about being pending, and
the first drive that finds it stale escalates it.

## Step timeout

Every arm node runs inside `agent.graph._with_step_timeout`
(`HELIX_STEP_TIMEOUT`, default 600s, `0` disables): a worker thread +
`future.result(timeout)` - portable to Windows, no signals, no DB-layer
timeout. A node still running at the deadline returns a `node_failed`
update; the coordinator turns it into a FAILED transition (the
rediagnosis edge + budget then apply like any other failure).

Tradeoff, explicitly: a timed-out worker cannot be killed - it runs to
completion in the background. Its returned update is discarded (the
timeout update already merged), but a slow transport may still write
after the run failed. The persisted `executions` rows remain the
authoritative record of what happened, so a late-arriving write is
auditable rather than hidden. This is the documented Phase 1 bound; a
cancellable transport contract is out of scope.

## Run concurrency and execution idempotency

Two layers stop duplicate work:

- **Run rows.** `run_incident` opens a `running` `workflow_runs` row
  under `SELECT ... FOR UPDATE` on the incident row, so two drivers
  serialize: the second either sees the committed `running` row
  (clean `RunConflictError`) or hits the partial unique index
  `ux_workflow_runs_one_running`. A suspended run resumes its own row,
  never opens a second.
- **Execution rows.** Every `executions` row carries
  `idempotency_key = {incident}:{fingerprint}:{attempt}:{kind}` where
  kind is `remediation` or `comp:{failed execution id}` -
  `ux_executions_idempotency` makes "one row per logical write" a
  database guarantee. The execute and compensate nodes look the key up
  before dispatch and replay the recorded row's outcome on a hit, so a
  crashed run re-entering a node cannot re-fire the transport. Resume
  also reloads persisted executions/verifications at intake, which is
  what makes a `verifying` or `executing` crash-resume decide from the
  recorded row rather than re-running the node.

## Escalation provenance

Escalation is a first-class outcome: `provenance.incident
.escalation_cause` surfaces the newest transition into `escalated`
(budget exhaustion, approval timeout, compensation failure,
irreversible verification failure, planner no-safe-action), the
Markdown rendering prints it in the incident header, and execution
entries carry `compensates_id` rendered as
`[compensation for execution #N]` - the undo link is visible wherever
executions are listed (`helix incidents show`, `/provenance`).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `HELIX_MAX_ATTEMPTS` | `3` | Failed diagnose->...->verify cycles per incident before escalation. |
| `HELIX_APPROVAL_TIMEOUT` | `86400` | Seconds a pending approvals row may sit before the sweep escalates; `0` disables. |
| `HELIX_STEP_TIMEOUT` | `600` | Wall-clock bound on one graph node inside a run; `0` disables. |
