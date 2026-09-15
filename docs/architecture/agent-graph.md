# Agent graph, coordinator, and diagnosis (subphases 1.5-1.7)

The workflow is a LangGraph `StateGraph` over `agent.state.WorkflowState`
- a flat, JSON-serializable TypedDict that is both the checkpointer
payload (D13) and the provenance substrate 1.9 assembles.

```text
START -> intake -> coordinator --> diagnosis --> coordinator --> END
                      ^    |                       ^    |
                      |    v                       |    |
                      +-- plan -> validate -> execute -> verify
                          (1.7: validate is real; execute/verify are
                          still slots routed by the coordinator)
```

## Nodes

| Node | Module | Does |
|---|---|---|
| `intake` | `agent/graph.py` | Loads the incident, its latest signal, and previously stored evidence into state; resets run-scoped fields. |
| `coordinator` | `agent/agents/coordinator.py` | Reads the incident's persisted status, applies the routing decision through `state.machine.transition()` (so every hop writes a transitions row), mirrors status back into state. |
| `diagnosis` | `agent/agents/diagnosis.py` | Collects evidence through `integrations.capabilities` protocols, runs the bounded request-more-evidence loop, interprets into `Hypothesis`, persists `evidence_items` + `hypotheses` rows. |
| `plan` | `agent/agents/planner.py` | (1.6) LLM proposes a `CandidateList` constrained to the action catalogue; a deterministic ranker orders it (dropping `excluded_candidates` fingerprints); the full list lands in `candidates` rows. See [action-catalogue.md](action-catalogue.md). |
| `validate` | `agent/agents/validation.py` + `agent/validation/` | (1.7) Re-validates the selected candidate's params, dispatches to a per-family strategy (kubernetes target, manifest dry-run, fix-PR content, pipeline existence), persists a `validations` row, and on FAIL adds the candidate fingerprint to `excluded_candidates`. Never executes the action. See [validation.md](validation.md). |
| `execute` / `verify` | placeholders | Registered slots that raise `NotImplementedError` until subphases 1.8-1.9 fill them in. |

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
| `validating` | verdict PASS or NOT_VALIDATABLE | none | END (1.8's policy node picks up from `validating`) |
| `failed` | - | `failed -> diagnosing` (rediagnosis; `attempt_count` bumped, stale hypothesis cleared) | `diagnosis` |
| later statuses, terminal | - | none | END |

`validating` is a resumable non-terminal status: a PASS or
NOT_VALIDATABLE run ends there and `helix run` on the incident reloads
the persisted selection and re-validates it. A FAIL verdict replans in
the same run - once; a second consecutive failure escalates (the
single-retry hook; full retry-budget machinery is 1.10).
NOT_VALIDATABLE is never treated as PASS: it means "proceed to the
policy/execution decision unchecked" and is recorded distinctly so
provenance can tell a validated candidate from an uncheckable one.

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

## Ablation arms (D8)

`HELIX_ARM` (`A|B|E`, default `E`) selects graph composition at build
time - `build_graph(deps, arm=...)` registers different node sets, not
code forks:

- **E** (full): `diagnosis` + `plan`/`validate`/`execute`/`verify` slots.
- **B** (single-call): `diagnose_and_plan` fuses diagnosis+planning into
  one LLM call; `execute` stays; validate/verify/replan are absent.
- **A** (rules): `rules_diagnosis`/`rules_plan` deterministic variants;
  the deterministic `validate` node is shared with E (it is pure code -
  no LLM), `execute`/`verify` stay placeholders.

Arm A/B rule-node variants are placeholders until 1.11; the composition
switch, the coordinator's per-arm `diagnosing` route, and the shared
`validate` node are what land now.

## Suspend/resume (D13)

`build_graph` compiles with a `PostgresSaver` checkpointer when
HELIX_DATABASE_URL is Postgres (`agent.graph.open_checkpointer`), with
`thread_id = incident_id`. That makes a suspended run resumable by any
process - `helix run`, `helix approve` (1.8), or the API - and gives the
`workflow_runs` single-active-run guard its substrate. Non-Postgres URLs
fall back to no checkpointer (state lives only for the invoke). Nothing
suspends in 1.5; the mechanism is wired and proven by the
integration-marked checkpoint round-trip test (`interrupt_before`).

## Runs

`agent.runner.run_incident(incident_id)` opens a `workflow_runs` row
(`running`), invokes the graph, and closes the row `terminal`,
`suspended` (graph parked mid-flow, or incident at `awaiting_approval`),
or `failed`. A second `running` run for one incident is refused - the
partial unique index `ux_workflow_runs_one_running` is the hard backstop.
A `suspended` run is resumed in place on the next `helix run`.

Entry points: `helix run INC-*`, `helix intake --run`, and
`POST /intake/*?run=true` - all drive the graph in-process (plan.md 1.4's
direct call; no event bus, C9).
