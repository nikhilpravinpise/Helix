# Benchmark methodology (subphase 1.11)

The controlled benchmark measures the recovery loop
(`diagnosis -> plan -> validate -> policy/approve -> execute -> verify`)
against the 12-scenario catalog in `benchmark/scenarios/`. Everything the
harness scores is produced by real node executions through the real CLI
code paths (`helix intake`, `helix run`) against durable Postgres state -
there is no in-harness shortcut, and the harness never fabricates a
result it did not observe.

## Arms (decision D8)

| Arm | Composition | Purpose |
|-----|-------------|---------|
| **E** | `diagnosis` + `plan` + `validate` + `execute` + `verify` + `compensate` | Full loop (needs `HELIX_LLM_API_KEY`) |
| **B** | `diagnose_and_plan` (one fused LLM call) + `execute` | Contrast arm: no validation, no verification, no replanning. The shared policy gate and executor still apply. |
| **A** | `rules_diagnosis` + `rules_plan` (deterministic tables, `agent/rules.py`) + shared `validate`/`execute`/`verify`/`compensate` | No-LLM baseline: signature -> hypothesis -> ordered action tables. |

`HELIX_ARM` selects node composition at graph-build time; it is not a
code fork. Arm B's fused node persists the hypothesis and the selected
candidate exactly like arm E's two nodes do, so the provenance timeline
exists for every arm.

### Arm B end state

Arm B has no `verify` node, so its incidents park at `verifying` after a
successful execution - `recovered` is structurally unreachable for arm B.
Its honest outcome column is `measured` (the harness's independent final-
state measurement); `recovered` is reported as it occurred and is 0 by
construction. A parked `verifying` status is not a crash - it is arm B's
designed stopping point (the benchmark harness measures actual state
itself). An arm-B run that reached `verifying` with a successful measured
execution "did the right write without verifying it" - the ablation's
point.

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

## Approvals (D2)

When a run suspends at `awaiting_approval`, the scripted approver
(`benchmark/approver.py`, actor `benchmark-approver`) decides the pending
row through `state.approvals.record_decision` - the same path
`helix approve` and the API use. It approves ONLY when the scenario
declares `approval_gated: true` and the pending action is the scenario's
`optimal_action`; anything else pending is rejected (which exercises the
real rejection -> replan/escalate path). Approving non-optimal gated
actions would silently bless arm mistakes, so it never happens.

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
