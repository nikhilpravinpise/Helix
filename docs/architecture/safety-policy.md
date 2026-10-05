# Safety policy, approval gate, and execution (subphase 1.8, coherence in 2.1)

This document is the safety story behind the write path: the access table
in code, the evidence-coherence check, the durable approval gate, and the two enforcement layers that
keep a wrong verdict from ever reaching a system.

```
validation verdict -> coordinator policy gate -> awaiting_approval --approve--> execute node
                    |                                 |                -> executor gate (2nd check)
                    |                                 +--reject--------->        -> transport write
                    +-- DENY -> replan              replan + exclude             -> executions row
```

## The access table as code

`agent/policy.py::decide(action, params, environment, evidence_types=None)` is pure - same
inputs, same verdict, no I/O. It returns a `PolicyVerdict`:

| Verdict | When | Effect |
|---|---|---|
| `ALLOW` | Read-only / no-op actions (none yet - every catalogue write is at least low-tier) | Execute |
| `ALLOW_WITH_CHECKS` | `low`/`medium` tier write in `sandbox`/`staging` | Execute; `verification_required` is set for `medium` (the 1.9 verifier needs the expected_effect to check against) |
| `REQUIRE_APPROVAL` | `high` tier anywhere, ANY write in `production` (or an unknown environment - fail-safe), a runtime-mutating action proposed on source-control-only evidence (coherence, below), or a cluster write to a namespace no registered service runs in (`namespace:` in the service registry, default the service name) | Park at `awaiting_approval` with a pending approvals row |
| `DENY` | Unknown action, params fail the catalogue's model, or a bound violation (protected branch, write root, manifest allowlist) | Replan with the fingerprint excluded; audited as a `refused` executions row |

Evidence coherence (2.1, the 1.5 ablation's open defect): when the caller
supplies `evidence_types` (canonical event types - signals classified
through `events.classify`, event records directly), a write must be
grounded in evidence from its own executor's domain, or it escalates to
REQUIRE_APPROVAL (`agent/policy.py::EXECUTOR_EVIDENCE`):

| Executor | Needs evidence of type |
|---|---|
| `kubernetes` (cluster) | `deployment.*`, `pod.failed`, `service.unhealthy` |
| `jenkins` (pipeline) | `build.*` |
| `github` (fix PR) | anything - a code fix is coherent with every domain |

The domain is the event TYPE, not the vendor source: a `build.failed` via a
GitHub check-run is CI evidence and lets `rerun_pipeline` proceed, while a
`pr.rejected` cannot authorize a cluster rollback. Cluster and pipeline were
one "runtime" set until 2026-10-03, when a failing unit test (`build.failed`)
let arm E restart and then roll back the live sandbox deployment unapproved.
CI evidence says nothing about the cluster. `None` skips the check (the
executor's re-decide relies on the coordinator's verdict).

Environment semantics: `decide` gates on the **incident's** target
environment (`signal.environment` -> `incidents.environment`), never on
`HELIX_ENVIRONMENT`. An environment outside `{sandbox, staging}` is
treated as approval-required rather than allowed - unknown is never
"unchecked write".

## Two enforcement layers (defense in depth)

The policy verdict is enforced **twice**, deliberately:

1. **Routing layer (coordinator).** `agent/agents/coordinator.py` computes
   the verdict at `validating` after a PASS/NOT_VALIDATABLE and routes:
   allowed -> `executing`, gated -> `awaiting_approval` + pending row,
   denied -> `remediation_pending` replan. The coordinator also reads the
   decided approvals row at `awaiting_approval` and at `executing`
   translates the persisted result into `verifying` / `failed`.
2. **Executor layer (`agent/execution`).** `enforce_execution_gate()`
   recomputes `decide()` and additionally loads the approvals row for the
   selected candidate's **fingerprint** before any transport call. DENY ->
   `ExecutionRefusedError`; REQUIRE_APPROVAL without a granted row ->
   `ExecutionRefusedError`; approved -> returns the approvals row so the
   executions audit can reference it. A run parked manually at
   `executing` - a routing bug, manual status tamper, or a stale
   checkpoint - is still refused here, because the gate does not trust
   the route that delivered it.

Below the executor sits a third, code-level bound inside the write
transports themselves: the kube client refuses contexts outside
`HELIX_ALLOWED_KUBE_CONTEXTS`, the github client refuses non-`helix/INC-*`
branches and non-`sandbox/payment-api/` paths, and the recording fakes
enforce the same bounds in fixture mode. Policy is a decision about
intent; these bounds are decisions about capability - each layer
re-asserts the boundary itself rather than trusting the layer above.

## The approval gate is durable and process-independent

A REQUIRE_APPROVAL verdict lands as a transaction that writes the
`approvals` row (`pending`) and the `validating -> awaiting_approval`
transition **together** - an incident can never sit at the gate without
a row to decide. The row carries `(action, params, fingerprint)` - the
identity the executor's gate re-checks by.

Deciding is a write, not a message: `helix approve INC-*` /
`helix reject INC-*` (or `POST /approvals/{id}/approve|reject` with
`{"actor": ...}`) flip the row to `approved`/`rejected` with
`decided_by`/`decided_at`/`note` via `state.approvals.record_decision`,
then drive `run_incident` to resume. A second decide on the row is a
clean error (`ApprovalDecidedError` -> CLI "already decided" / API 409) -
the gate can never double-fire.

- **Approved** -> `awaiting_approval -> executing`; the executor's gate
  finds the granted row and the executions row carries
  `approval_id` + `decided_by` - WHO blessed the write is on the audit
  record.
- **Rejected** -> `awaiting_approval -> remediation_pending` with the
  rejected fingerprint added to `excluded_candidates` (unioned with the
  persisted rejected rows on every intake reload) - the planner can never
  re-propose what a human refused.
- **Still pending** -> the run stays suspended; `helix run` ends it
  suspended again.

`HELIX_OPERATOR` supplies the default CLI approver identity;
`--actor`/the API body override it. Approval is never anonymous.

## The executions audit row

Every attempt - allowed, approved, refused, errored - writes one
`executions` row: action + params + fingerprint, the policy decision it
ran under, `result` (`success`/`error`/`refused`), the transport output
(for fakes: the recorded intended write), `origin` (`live`/`fixture`),
timestamps, and the approval link. Policy-level DENYs are audited the
same way, so "what was attempted under which decision" is always
queryable per incident.

## Resume mechanics

`awaiting_approval` is a clean stop, not an interrupt: the graph ends
with no pending nodes, and the resume re-enters at `intake`, which
reloads the persisted selection, the hypothesis, the exclusions union,
and the latest executions row for the selected fingerprint. The
coordinator then decides from `status + approvals row`. That is why the
gate survives a process restart - and why it works even with no
checkpointer: the durable pieces are Postgres rows, not graph memory.

Executor-side replay safety: `executing -> verifying` transitions happen
only for `result == "success"`; an `error` or `refused` result goes to
`failed` with the fingerprint excluded, so a rediagnosed plan cannot
blindly repeat it.

## Fixture mode (D10)

`HELIX_EVIDENCE_SOURCE=fixtures` selects the recording fakes
(`integrations/fakes.py`) for writes as well as reads. A fixture-mode run
ends at `verifying` with `executions.output` holding the intended write
(`recorded: true, origin: "fixture"`) - the benchmark can inspect exactly
what Helix would have done without any cluster, repo, or Jenkins ever
being touched. The fakes enforce the same code-level bounds (context
allowlist, branch/path rules) so fixture runs exercise real refusals.

## Config surface

| Setting | Default | Meaning |
|---|---|---|
| `HELIX_ALLOWED_KUBE_CONTEXTS` | `kind-helix-sandbox` | Comma-separated kube-context allowlist for ALL kube writes (fake and live). |
| `HELIX_OPERATOR` | `operator` | Default approver identity for `helix approve`/`reject`. |
