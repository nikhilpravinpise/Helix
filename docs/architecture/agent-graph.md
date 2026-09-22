# Agent graph, coordinator, and diagnosis (subphases 1.5-1.10)

The workflow is a LangGraph `StateGraph` over `agent.state.WorkflowState`
- a flat, JSON-serializable TypedDict that is both the checkpointer
payload (D13) and the provenance substrate 1.9 assembles.

```text
START -> intake -> coordinator --> diagnosis --> coordinator --> END
                      ^    |                       ^    |
                      |    v                       |    |
                      +-- plan -> validate -> execute -> verify -> compensate
                          (1.8: execute is real behind the coordinator's
                          policy gate; verify checks desired state (1.9);
                          compensate undoes a verification-failed
                          compensable change (1.10))
```

## Nodes

| Node | Module | Does |
|---|---|---|
| `intake` | `agent/graph.py` | Loads the incident, its latest signal, and previously stored evidence into state; resets run-scoped fields. |
| `coordinator` | `agent/agents/coordinator.py` | Reads the incident's persisted status, applies the routing decision through `state.machine.transition()` (so every hop writes a transitions row), mirrors status back into state. |
| `diagnosis` | `agent/agents/diagnosis.py` | Collects evidence through `integrations.capabilities` protocols, runs the bounded request-more-evidence loop, interprets into `Hypothesis`, persists `evidence_items` + `hypotheses` rows. |
| `plan` | `agent/agents/planner.py` | (1.6) LLM proposes a `CandidateList` constrained to the action catalogue; a deterministic ranker orders it (dropping `excluded_candidates` fingerprints); the full list lands in `candidates` rows. See [action-catalogue.md](action-catalogue.md). |
| `validate` | `agent/agents/validation.py` + `agent/validation/` | (1.7) Re-validates the selected candidate's params, dispatches to a per-family strategy (kubernetes target, manifest dry-run, fix-PR content, pipeline existence), persists a `validations` row, and on FAIL adds the candidate fingerprint to `excluded_candidates`. Never executes the action. See [validation.md](validation.md). |
| `execute` | `agent/agents/execution.py` + `agent/execution/` | (1.8) Re-checks the policy verdict (executor-side gate), dispatches to the action-family executor, and persists one `executions` audit row per attempt. The transports are `integrations/writes.py` protocols - live clients or recording fakes depending on `HELIX_EVIDENCE_SOURCE`. See [safety-policy.md](safety-policy.md). |
| `verify` | `agent/agents/verification.py` + `agent/verification/` | (1.9) Per-action desired-state checks sampled across a stability window; persists a `verifications` row; never writes. See [verification.md](verification.md). |
| `compensate` | `agent/agents/compensation.py` | (1.10) A failed verification on a compensable action routes here first: derives the catalogue's `compensating_action` (or the executor-side `close_pr` op), dispatches it through the executor layer, persists the attempt as an `executions` row with `compensates_id` set. See [unhappy-paths.md](unhappy-paths.md). |

## Coordinator owns every transition

Worker nodes never call `machine.transition()` - they write outputs
(evidence, hypothesis, errors) into `WorkflowState` and return to the
coordinator, which alone decides the next status. The decision table
(1.5-1.7):

| Persisted status | Condition | Transition | Next node |
|---|---|---|---|
| `received` | - | `received -> diagnosing` | `diagnosis` |
| `diagnosing` | diagnosis not yet run | none | `diagnosis` (crash-resume path) |
| `diagnosing` | hypothesis recorded | `diagnosing -> remediation_pending` | `plan` |
| `diagnosing` | diagnosis done, no hypothesis | `diagnosing -> failed` | END |
| `remediation_pending` | planner not yet run | none | `plan` |
| `remediation_pending` | `selected_action` set | `remediation_pending -> validating` | `validate` |
| `remediation_pending` | `plan_complete`, no selection | `remediation_pending -> escalated` (cause recorded) | END |
| `remediation_pending` | `plan_failed` (proposal call errored) | `remediation_pending -> failed` | END |
| `validating` | no verdict in state yet | none | `validate` |
| `validating` | verdict FAIL, first validation pass | `validating -> remediation_pending` (plan outputs cleared; failed fingerprint is in `excluded_candidates`) | `plan` (replan) |
| `validating` | verdict FAIL, second pass | `validating -> escalated` (single retry exhausted) | END |
| `validating` | verdict PASS / NOT_VALIDATABLE, policy ALLOW_WITH_CHECKS | `validating -> executing` | `execute` |
| `validating` | verdict PASS / NOT_VALIDATABLE, policy REQUIRE_APPROVAL | `validating -> awaiting_approval` (pending approvals row persisted in the same transaction) | END (suspended until decided) |
| `validating` | verdict PASS / NOT_VALIDATABLE, policy DENY | `validating -> remediation_pending` (fingerprint excluded; refused executions row) | `plan` (replan) |
| `awaiting_approval` | approvals row approved | `awaiting_approval -> executing` | `execute` |
| `awaiting_approval` | approvals row rejected | `awaiting_approval -> remediation_pending` (fingerprint excluded) | `plan` (replan) |
| `awaiting_approval` | approvals row still pending | none | END (suspended) |
| `executing` | no executions row in state yet | none | `execute` |
| `executing` | executions row `success` | `executing -> verifying` | `verify` |
| `executing` | executions row `refused` | `executing -> remediation_pending` (in-run replan, 2.1; arms with a planner) | `plan` (replan) |
| `executing` | executions row `error` | `executing -> failed` (fingerprint excluded) | END |
| `verifying` | no verification in state yet | none | `verify` |
| `verifying` | verification `pass` | `verifying -> recovered` | END |
| `verifying` | verification `fail`, irreversible action | `verifying -> escalated` | END |
| `verifying` | verification `fail`, compensable action, debt unpaid | none | `compensate` |
| `verifying` | compensation `error` / `refused` | `verifying -> escalated` (un-undone change needs a human) | END |
| `verifying` | verification `fail`, stateless or compensated, budget left | `verifying -> remediation_pending` (attempt consumed; fingerprint excluded) | `plan` (replan) |
| `verifying` | any fail with `attempt_count` spent | `verifying -> escalated` ("retry budget exhausted") | END |
| `failed` | attempts remain | `failed -> diagnosing` (rediagnosis; `attempt_count` bumped, stale hypothesis cleared) | `diagnosis` |
| `failed` | `attempt_count` spent | `failed -> escalated` ("retry budget exhausted") | END |
| any | `node_failed` set (step timeout / node crash) | `* -> failed` | END |

A FAIL verdict replans in the same run - once; a second consecutive
failure escalates (the single-retry hook). Failed verify cycles and
`failed` re-entries consume `incidents.attempt_count` against
`HELIX_MAX_ATTEMPTS` (1.10). NOT_VALIDATABLE is never treated as PASS:
it means "proceed to the policy/execution decision unchecked" and is
recorded distinctly so provenance can tell a validated candidate from
an uncheckable one. The policy gate rows are the 1.8 access table - see
[safety-policy.md](safety-policy.md) for the verdict rules and the
executor-side second enforcement layer; [unhappy-paths.md](unhappy-paths.md)
for the 1.10 bounds (compensation, budgets, timeouts, idempotency).

## Phase 2 change lifecycle (2.1/2.5/2.6/2.9)

`change` incidents (`Incident.kind=change`, PR/push events) do not enter
the recovery nodes above: the coordinator drives `received -> triaged ->
owner_assigned -> review_pending -> approved/rejected -> fix_required ->
deploying -> recovered` through the Triage and Review agents, with a
failed deploy handing off to the Ops loop at `diagnosing`. The edge map
lives in [state-machine.md](state-machine.md); this document does not
duplicate it.

## Diagnosis evidence flow

1. **Deterministic pre-selection** by signal source - kubernetes:
   pods + events + secret_info (for every Secret the pods reference) +
   service logs; jenkins: console + test_results + the build's diff when
   the signal carries a commit sha; github: commit/diff/check_runs when a
   sha is known. Unavailable pieces are recorded in `state.errors`,
   never fabricated.
2. **Bounded request-more-evidence loop** - the LLM sees evidence
   summaries only (`id | source | kind | summary` lines; full payloads
   stay retrievable by id, so prompts stay bounded) and may request items
   from a fixed requestable menu, capped at `HELIX_MAX_EVIDENCE_ROUNDS`
   (default 3).
3. **Interpretation** via `complete_structured` into `Hypothesis(claim,
   evidence_refs, confidence, contradicting_signals)`. Every cited ref
   must resolve to a collected evidence id; a bad citation gets one
   re-prompt naming the rejected ids, then is recorded with confidence
   clamped to 0.2 and dangling refs dropped (full unhappy-path handling
   is 1.10).

Every collected `Evidence` lands in `evidence_items` (unique per
incident on its content-hash `evidence_id`) and each hypothesis in
`hypotheses` - "the full evidence chain is reconstructable from stored
state" is a DB query.

## Ablation arms (D8, extended in E6/2.8/2.11)

`HELIX_ARM` (`A|B|C|D|E`, default `E`) selects graph composition at build
time - `build_graph(deps, arm=...)` registers different node sets, not
code forks:

- **E** (full): `diagnosis` + `plan`/`validate`/`execute`/`verify` slots (Phase 2: the Ops Agent boundary owns this loop; change incidents route through triage/review instead).
- **D** (event + graph context, 2.8): `diagnose_and_plan` fed the composed knowledge context (E6 retrieval + event timeline + graph neighborhood) + `execute`.
- **C** (retrieval, E6): `diagnose_and_plan` with retrieval context + `execute` - same fused node as B, fed retrieved evidence.
- **B** (single-call): `diagnose_and_plan` fuses diagnosis+planning into
  one LLM call; `execute` stays; validate/verify/replan are absent.
- **A** (rules): `rules_diagnosis`/`rules_plan` deterministic variants
  (agent/rules.py, 1.11 - signature -> hypothesis -> ordered action
  tables, zero LLM calls); `validate`/`execute`/`verify`/`compensate`
  are shared with E - they are pure code, not arm-specific reasoning.

1.11 filled the arm A/B variants and made the coordinator arm-aware: at
`validating` arm B skips the absent validate node straight into the
shared policy gate, and edges that would need a replan (policy DENY,
approval REJECTED) escalate under B since no replan node exists. Arm B
parks at `verifying` after a successful execute - its designed end
state; the benchmark measures final state itself.

## Suspend/resume (D13)

`build_graph` compiles with a `PostgresSaver` checkpointer when
HELIX_DATABASE_URL is Postgres (`agent.graph.open_checkpointer`), with
`thread_id = incident_id`. Two suspension shapes exist:

- **Mid-graph interrupt** (test-only today, `interrupt_before`): the
  checkpoint has pending nodes and the resume invokes with no input.
- **The approval gate** (1.8): `awaiting_approval` is a clean stop - the
  graph completes with nothing pending, and the resume re-enters at
  `intake`, which reloads the persisted selection, hypothesis, evidence,
  exclusion union, and the latest executions row. The coordinator then
  decides from `status + approvals row`, so the gate works even with no
  checkpointer at all - the durable pieces are Postgres rows, not graph
  memory.

That makes a suspended run resumable by any process - `helix run`,
`helix approve`, or the API - and gives the `workflow_runs`
single-active-run guard its substrate. Non-Postgres URLs fall back to no
checkpointer (state lives only for the invoke; the approval-resume path
still works because intake rebuilds state from the rows).

## Runs

`agent.runner.run_incident(incident_id)` opens a `workflow_runs` row
(`running`), invokes the graph, and closes the row `terminal`,
`suspended` (graph parked mid-flow, or incident at `awaiting_approval`),
or `failed`. A second `running` run for one incident is refused - the
partial unique index `ux_workflow_runs_one_running` is the hard backstop.
A `suspended` run resumes on the next `helix run`: in place when the
checkpoint has pending nodes, otherwise by re-entry at `intake`.

Entry points: `helix run INC-*`, `helix intake --run`, and
`POST /intake/*?run=true` - all drive the graph in-process (plan.md 1.4's
direct call; no event bus, C9).
