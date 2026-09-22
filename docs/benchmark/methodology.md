# Benchmark methodology (subphase 1.11, expanded in 2.11)

The controlled benchmark measures the recovery loop
(`diagnosis -> plan -> validate -> policy/approve -> execute -> verify`)
and, for `flow: change` scenarios (2.11), the change-review lifecycle
(`triaged -> owner_assigned -> review_pending -> approved/rejected ->
fix_required -> deploying -> recovered`), against the 19-scenario catalog in `benchmark/scenarios/`. Everything the
harness scores is produced by real node executions through the real CLI
code paths (`helix intake`, `helix run`) against durable Postgres state -
there is no in-harness shortcut, and the harness never fabricates a
result it did not observe.

## Arms (decision D8)

| Arm | Composition | Purpose |
|-----|-------------|---------|
| **E** | `diagnosis` + `plan` + `validate` + `execute` + `verify` + `compensate` | Full loop (needs `HELIX_LLM_API_KEY`) |
| **D** | `diagnose_and_plan` fed the composed graph context (retrieval + event timeline + graph neighborhood, 2.8) + `execute` | Event/graph-context arm: is the composed knowledge context better than retrieval alone? |
| **C** | `diagnose_and_plan` with retrieval context (E6) + `execute` | RAG arm: same fused node as B, fed retrieved evidence. |
| **B** | `diagnose_and_plan` (one fused LLM call) + `execute` | Contrast arm: no validation, no verification, no replanning. The shared policy gate and executor still apply. |
| **A** | `rules_diagnosis` + `rules_plan` (deterministic tables, `agent/rules.py`) + shared `validate`/`execute`/`verify`/`compensate` | No-LLM baseline: signature -> hypothesis -> ordered action tables. |

`HELIX_ARM` selects node composition at graph-build time; it is not a
code fork. Arm B's fused node persists the hypothesis and the selected
candidate exactly like arm E's two nodes do, so the provenance timeline
exists for every arm.

### Arm B end state

Arm B has no `verify` node, so its incidents park at `verifying` after a
successful execution - `recovered` is structurally unreachable for arm B
(and arms C and D share the same fused-node shape, so the same holds).
Its honest outcome column is `measured` (the harness's independent final-
state measurement); `recovered` is reported as it occurred and is 0 by
construction. A parked `verifying` status is not a crash - it is arm B's
designed stopping point (the benchmark harness measures actual state
itself). An arm-B run that reached `verifying` with a successful measured
execution "did the right write without verifying it" - the ablation's
point.

### Cross-arm contamination and isolation

A multi-arm invocation (`--arm B --arm C --arm E`, for instance) runs
every arm against the SAME Postgres with the SAME scenario catalog, so
each arm emits the identical signal for a given scenario that every other
arm emits. `state/intake.py`'s dedup is fingerprint-based
(`source | service | environment | signature`) and correctly attaches a
repeated signal to any OPEN (non-terminal) incident sharing that
fingerprint - that is Phase 1 dedup working exactly as designed, not a
bug to route around.

This was observed for real: running `--arm B --arm C` in one invocation
left arm B's incidents parked at `verifying` (arm B's designed end state,
above - not terminal). Arm C then ran the same scenarios, and its
identical signals attached to arm B's still-open incidents instead of
opening their own. An incident at `verifying` does no further LLM work,
so several of arm C's scenarios reported hypothesis, selected-action and
verification data that arm B had produced - arm C's scores for those
scenarios were inherited, not measured, and looked completely plausible
in the report.

The fix lives entirely in the harness, not in dedup: `run_benchmark`
tracks every incident an arm's scenarios touched and, once that arm's
full pass is done and another arm is still queued behind it, force-closes
any of those incidents that are not already terminal
(`benchmark/harness.py:_close_arm_incidents`, via the same validated
`state.machine.transition` every other status change in Helix uses - the
same effect open_incident dedup respects). A single-arm invocation never
calls this, so single-arm behavior is unchanged. It only protects against
contamination WITHIN one invocation; it does not clean up incidents left
open by an earlier, separate benchmark run - a fresh database (or a
manual truncate) between full benchmark runs is still an operator
responsibility. Anyone reading a future multi-arm ablation table should
know this was the failure mode caught and closed before real
(non-arm-A) numbers were ever produced.

## Injection modes (D10)

- **`inject: live`** runs `scripts/<fault_script>` against the kind
  sandbox, then feeds the scenario's signal. Requires `kind` (the
  reachable `kind-helix-sandbox` context), plus `elasticsearch` where the
  evidence layer needs it.
- **`inject: fixture`** sets `HELIX_EVIDENCE_SOURCE=fixtures` +
  `HELIX_FIXTURE_PATH=<evidence_fixture>` for the `helix run`/`helix
  intake` subprocesses; evidence comes from
  `fixtures/faults/<fixture>.json` and writes land on the recording fakes
  (`origin="fixture"`). The recorded intended write IS the measured state
  for fixture scenarios - the harness scores the recorded output, not a
  pretend live mutation.

Scenarios whose `requires` are unmet are recorded as **skipped** and
excluded from every metric's numerator and denominator - a skipped run is
not a failed run, and it can never inflate a pass rate. On this host the
live scenarios are skipped (`kind`/ES absent); they become runnable on a
host with the sandbox stack.

## Approvals (D2) and change-flow reviews (2.11)

When a run suspends at `awaiting_approval`, the scripted approver
(`benchmark/approver.py`, actor `benchmark-approver`) decides the pending
row through `state.approvals.record_decision` - the same path
`helix approve` and the API use. It approves ONLY when the scenario
declares `approval_gated: true` and the pending action is the scenario's
`optimal_action`; anything else pending is rejected (which exercises the
real rejection -> replan/escalate path). Approving non-optimal gated
actions would silently bless arm mistakes, so it never happens.

When a change-flow run parks at `review_pending`, the scripted reviewer
(`benchmark/reviewer.py`) decides the pending review row through
`state.reviews.record_decision` - the same path `helix review` and the
API use - from the scenario's `review_decision` field (per-round
approve/reject, or `timeout` for no answer). Change scenarios carry
ordered canonical `events` (not a single `signal`); see
`benchmark/catalog.py` for the required fields.

## Verification and fixture evidence

Fixture-mode verification runs the real checks against fixture-backed
capabilities. Legs the fixture cannot support (e.g. a functional check
against a cluster that does not exist) are recorded as `skipped` in the
verifications row, never as passed; a verification only passes when at
least one check ran and every non-skipped check passed. Frozen fixtures
mean k8s-runtime actions re-observe the still-broken world - on fixture
scenarios that is honest behavior (the write was recorded, not applied),
and it is why the six fixture scenarios all remediate through `open_fix_pr`
or `rerun_pipeline`, whose verifiable legs (PR recorded / Jenkins build
green) the fixtures do carry.

## Reports

`benchmark/harness.py` writes `benchmark-<ts>-<arms>.json` (machine-
readable) and `.md` (the section-17 metrics table) under
`benchmark/reports/`. The latest run's artifacts are committed as
`latest.json`/`latest.md` pointers; timestamped intermediates are
gitignored. Each report carries run metadata (arms, mode, generated_at),
the per-scenario table, and the per-arm metric rollup.

## Honest-score rules the scorer enforces

- Skipped scenarios score nothing - `skipped` rows carry a reason and no
  metrics.
- `rca_match` uses the deterministic token-group matcher in
  `benchmark/scoring.py` (`ROOT_CAUSE_TOKENS[fault]`) applied to the
  persisted hypothesis claim - that is what lets arm A score on RCA
  accuracy with no LLM. When `HELIX_LLM_API_KEY` is present, a semantic
  judge can replace it; the deterministic matcher is the default.
- `unsafe_executions` counts non-compensation execution rows whose action
  is in `unsafe_actions`, or a `require_approval` action that succeeded
  without a granted approval row - the second clause is defense-in-depth
  against a bypassed gate.
- `recovered` requires the incident to reach `recovered` status AND the
  harness's measured final-state checks to all pass.
- `mttr_seconds` is the DB record: `recovered` transition timestamp minus
  the signal's persisted `created_at`.
