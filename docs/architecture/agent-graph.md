# Agent graph, coordinator, and diagnosis (subphase 1.5)

The workflow is a LangGraph `StateGraph` over `agent.state.WorkflowState`
- a flat, JSON-serializable TypedDict that is both the checkpointer
payload (D13) and the provenance substrate 1.9 assembles.

```text
START -> intake -> coordinator --> diagnosis --> coordinator --> END
                      ^    |                       ^    |
                      |    v (later subphases)     |    |
                      +-- plan -> validate -> execute -> verify
                          routed by the coordinator as they land
```

## Nodes

| Node | Module | Does |
|---|---|---|
| `intake` | `agent/graph.py` | Loads the incident, its latest signal, and previously stored evidence into state; resets run-scoped fields. |
| `coordinator` | `agent/agents/coordinator.py` | Reads the incident's persisted status, applies the routing decision through `state.machine.transition()` (so every hop writes a transitions row), mirrors status back into state. |
| `diagnosis` | `agent/agents/diagnosis.py` | Collects evidence through `integrations.capabilities` protocols, runs the bounded request-more-evidence loop, interprets into `Hypothesis`, persists `evidence_items` + `hypotheses` rows. |
| `plan` / `validate` / `execute` / `verify` | placeholders | Registered slots that raise `NotImplementedError` until subphases 1.6-1.9 fill them in. |

## Coordinator owns every transition

Worker nodes never call `machine.transition()` - they write outputs
(evidence, hypothesis, errors) into `WorkflowState` and return to the
coordinator, which alone decides the next status. The 1.5 decision table:

| Persisted status | Condition | Transition | Next node |
|---|---|---|---|
| `received` | - | `received -> diagnosing` | `diagnosis` |
| `diagnosing` | diagnosis not yet run | none | `diagnosis` (crash-resume path) |
| `diagnosing` | hypothesis recorded | `diagnosing -> remediation_pending` | END |
| `diagnosing` | diagnosis done, no hypothesis | `diagnosing -> failed` | END |
| `failed` | - | `failed -> diagnosing` (rediagnosis; `attempt_count` bumped, stale hypothesis cleared) | `diagnosis` |
| `remediation_pending`, later statuses, terminal | - | none | END |

`remediation_pending` is the honest 1.5 end state: a hypothesis exists
and the run stops there until the planner lands (1.6).

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
  the deterministic validate/execute/verify slots are shared with E.

Arm A/B node variants are placeholders until 1.11; the composition switch
and the coordinator's per-arm `diagnosing` route are what land now.

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
