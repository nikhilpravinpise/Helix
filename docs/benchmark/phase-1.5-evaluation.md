# Phase 1.5 evaluation (subphase E7)

This document reports what was actually measured for E7 - "measure what
the event layer bought" - against the state of this machine on the date
below. It does not round away detail and it does not fill in a number
that was not produced by a real run.

- Direct-path report: `benchmark/reports/e7-direct.json` (generated
  2026-09-19T18:21:42Z)
- Event-path report: `benchmark/reports/e7-event.json` (generated
  2026-09-19T18:22:04Z)
- Both produced by `bash scripts/benchmark.sh --path direct` /
  `--path event`, arm A, `--mode auto`, against the 12-scenario catalog.
- Event-layer numbers below are from `bash scripts/event-metrics.sh`, run
  immediately after both benchmark runs.
- Infrastructure present on this machine: Postgres (`helix-postgres`),
  Kafka (`helix-kafka`). No `kind` cluster, no Elasticsearch. **No
  `HELIX_LLM_API_KEY`.**

---

## 1. Event-layer measurements

`event_layer_report` scores everything currently durable in this
Postgres instance, not only the events this evaluation's runs produced
(see `events/metrics.py`'s module docstring - this is by design, so a
whole-store consistency check is what "rerun the benchmark through the
event path and see if anything drifted" needs). This store already held
14 incidents and 7 non-Helix event records before today's runs; the
numbers below include that history. Where that matters for what a number
proves, it is called out.

| metric | rate | numerator / denominator | notes |
|---|---|---|---|
| `correlation_accuracy` | **not measured** | 0 / 0 | see below |
| `duplicate_handling_rate` | 1.00 | 114 / 114 | `redeliveries = 0` - see below |
| `event_latency` (non-Helix) | n/a (distribution) | count = 13 | see below |
| `replay_correctness` | 1.00 | 26 / 26 | `not_replayable = 0` |

**`correlation_accuracy` was not run and is not a 0/0-means-broken
situation - it needs ground truth this invocation was never given.**
`events.metrics.correlation_accuracy` takes `expected_groups` as an
explicit argument (there is no way to derive "these events should share
an incident" from the database itself); `scripts/event-metrics.sh` calls
`event_layer_report` with no `expected_groups`, so the report honestly
returns `rate: null, denominator: 0` with the note `"not measured: no
expected_groups (ground truth) provided"`. The behavior this metric is
meant to check - a GitHub check, a Jenkins build, a Kubernetes pod and an
ELK log line converging on one incident - is exercised and asserted at
the unit level (`tests/events/test_correlation.py`,
`tests/events/test_metrics.py::TestCorrelationAccuracy`, both against the
four-source chain described in D19), but no scenario-specific ground
truth is wired into the CLI, so no number for it exists from a live run.
Wiring that up was out of scope for this evaluation and would need the
benchmark catalog to declare, per correlation scenario, which event ids
are expected to land on one incident.

**`duplicate_handling_rate` = 1.00 over `redeliveries = 0` proves
nothing about duplicate handling.** The module docstring for this metric
says so explicitly: "a rate of 1.00 over zero redeliveries says only
that nothing was redelivered." Nothing in today's runs or in this
store's history has ever redelivered an event id, so this number
confirms only that `event_records.event_id`'s unique index is intact
against events that were each ingested exactly once - which is expected
and uninteresting. The actually-informative property (that a *redelivered*
event creates no second incident, no extra correlation token, and no
extra transition) is asserted directly by
`tests/events/test_ingest.py::TestReplay`, not by this rate. If a real
redelivery test is wanted from a live run, it would mean deliberately
re-ingesting an event id and re-running this metric to see `redeliveries
> 0` - not done here.

**`event_latency` (13 non-Helix records, 101 Helix lifecycle records
excluded) measures the local ingest-to-write path, not an end-to-end
pipeline including Kafka.** `benchmark/harness.py`'s event path
(`_ingest_via_events`) calls `events.ingest.ingest_event` directly in the
harness process - it does not publish to Kafka and consume back. So the
sub-40ms latencies below are `created_at - occurred_at` for a
function call and a database write, not a measurement of a real
producer/consumer round trip through the broker:

- count: 13, excluded (Helix-sourced): 101
- median: 0.0112s, mean: 0.0154s, p95: 0.0332s, min: 0.0071s, max: 0.0368s

An end-to-end latency number that includes the broker would require
driving events through `events.broker.EventProducer` and a live consumer
group, which this evaluation did not do.

**`replay_correctness` = 1.00 over 26 incidents, 0 not-replayable.**
This is the one event-layer number that reads as a straightforward pass
on its own terms: every incident in the store, whether created via the
direct path or the event path, replays to the identical
`IncidentProjection` as its live state. It is a whole-store number (26
incidents, not just today's 12), which is appropriate here since replay
correctness is a property that should hold for every incident, not just
new ones.

---

## 2. Direct-path vs event-path comparison

Both reports: arm A only, 6 of 12 scenarios run (`bad-deployment-config`,
`config-mismatch`, `crashloop`, `failed-rollout`, `missing-secret`,
`oom-killed` skipped - `unmet requirements: kind[, elasticsearch]`, which
is expected and correct on this machine).

`uv run python -m benchmark.compare benchmark/reports/e7-direct.json benchmark/reports/e7-event.json`
exit code: **0** (no regression).

| arm | metric | direct | event | delta | comparable | regressed |
|---|---|---|---|---|---|---|
| A | rca_accuracy | 1.0000 | 1.0000 | 0.0000 | yes | no |
| A | action_validity | 1.0000 | 1.0000 | 0.0000 | yes | no |
| A | recovery_success | 1.0000 | 1.0000 | 0.0000 | yes | no |
| A | measured_state_match | 1.0000 | 1.0000 | 0.0000 | yes | no |
| A | unsafe_action_rate | 0.0000 | 0.0000 | 0.0000 | yes | no |

verdict: **NO REGRESSION**, incomparable arm/metric pairs: 0.

**A metric `benchmark.compare` did not track, and the hole it hid.** The
first event-path run reported `mttr_seconds_median = null` on every one
of the 6 recovered scenarios, against `1.4845` on the direct path - and
the comparison still said NO REGRESSION, because MTTR is not in
`benchmark/compare.py`'s gated `METRICS`. A metric silently becoming
unmeasurable is exactly what that verdict exists to catch, so this was a
defect in the harness rather than a property of the event path.

The cause was structural. `benchmark/harness.py::_collect` derived
`signal_at` from `incident.signals[-1].created_at`, and an event-path
incident has no `Signal` row by design: `events.ingest.ingest_event`
never writes one, and `agent/graph.py::_signal_from_events` reconstructs
the signal view from the stored event envelope instead (decision D22).
So `signal_at` was always `None` and MTTR could never be computed.

Fixed in `_first_observed_at`: when there is no signal row the clock
starts at the earliest non-Helix `EventRecord.occurred_at`. Helix's own
lifecycle events are excluded, because they are stamped when Helix
reacted - starting the clock at one would measure from Helix's response
rather than from the failure and flatter MTTR. With that in place both
paths are measurable and agree:

| metric | direct | event |
|---|---|---|
| `mttr_seconds_median` | 1.4845 | 1.4752 |

MTTR remains outside the regression gate on purpose, per
`docs/benchmark/tolerance.md`: on live scenarios it measures cluster
settle time and kubelet backoff rather than Helix. It is reported so a
shift stays visible, which is how this was caught.

---

## 3. Ablation table: arms A, B, C, E

| metric | Arm A (rules, measured) | Arm B (single LLM) | Arm C (LLM + retrieval) | Arm E (full Helix) |
|---|---|---|---|---|
| scenarios attempted / skipped / run | 12 / 6 / 6 | not measured | not measured | not measured |
| `rca_accuracy` | 1.00 | not measured | not measured | not measured |
| `action_validity` | 1.00 | not measured | not measured | not measured |
| `recovery_success` | 1.00 | not measured | not measured | not measured |
| `measured_state_match` | 1.00 | not measured | not measured | not measured |
| `unsafe_action_rate` | 0.00 | not measured | not measured | not measured |
| `human_intervention_rate` | 0.00 | not measured | not measured | not measured |
| `mttr_seconds_median` | 1.4845 (direct) / 1.4752 (event) | not measured | not measured | not measured |

**Arms B, C, and E are marked `not measured` because there is no
`HELIX_LLM_API_KEY` on this machine.** All three arms require LLM calls
(B: fused diagnose+plan; C: B plus E6 retrieval; E: the full
diagnose/plan/validate/execute/verify/replan graph) and none can run
without it. No number above for B, C, or E is a placeholder, an
estimate, or carried over from a different run - they were not attempted,
`benchmark/tolerance.py` already marks their non-structural bands
`PROVISIONAL` for exactly this reason, and this document does not narrow
that.

---

## 4. What this evaluation does and does not establish

**What it establishes:**

- The event ingestion path (`events.ingest.ingest_event`) drives the same
  recovery graph to the same outcomes as the direct CLI path, on arm A,
  for the 6 scenarios this machine can run live: identical `rca_accuracy`,
  `action_validity`, `recovery_success`, `measured_state_match`, and
  `unsafe_action_rate`.
- Replay reconstructs every incident in the store (26/26) identically to
  its live state, across both intake paths.
- The unique-index guarantee behind duplicate handling holds against
  every event id ever ingested here (114/114), though see section 1 for
  why that number is weaker evidence than it looks.

**What it does not establish:**

- **Arm A's 1.00s are not evidence that Helix's reasoning works.** Arm A
  is the deterministic rules baseline: `agent/rules.py` maps a fault
  signature straight to a hypothesis and an action through a lookup
  table built from the same fault catalogue the benchmark scores against.
  A 1.00 from arm A is close to a lookup table returning its own answer
  key - it demonstrates the harness, the state machine, the policy gate,
  and the event/direct intake paths all work end to end, and nothing
  about whether an LLM-driven agent can diagnose or plan correctly under
  uncertainty.
- **No claim about the agentic architecture (arms B, C, E) is supported
  by this document.** They did not run. There is no result, provisional
  or otherwise, that says the LLM-driven graph diagnoses correctly,
  plans safely, or benefits from retrieval - only that the wiring
  (`HELIX_ARM` selection, the shared validate/execute/verify machinery)
  exists and is exercised by arm A's runs through the same graph
  construction path.
- Correlation accuracy - the specific thing E4 built and E7 was asked to
  measure - has no number from a live run in this document. It is
  unit-tested, not benchmarked here.
- MTTR was unmeasurable on the event path until `_first_observed_at`
  landed during this evaluation, and the comparison reported NO
  REGRESSION throughout, because MTTR is not a gated metric. Both paths
  are measurable now and agree (1.4845 direct, 1.4752 event), but the
  episode is the finding: a verdict only covers what it gates.
- Duplicate handling is unverified under an actual redelivery in this
  evaluation - it has never had one to check.
- Event latency here reflects an in-process function call, not a
  Kafka-mediated pipeline.

---

## 5. Exit criterion status

plan.md E7's exit criterion: *"no regression against Phase 1 numbers,
correlation and replay metrics recorded, and arm C added to the ablation
table."*

- **No regression against Phase 1 numbers:** met, for the metrics
  `benchmark.compare` checks, on the scenarios this machine can run
  (section 2). Not evaluated for MTTR, which is unmeasurable on the
  event path as it stands.
- **Correlation and replay metrics recorded:** replay, yes (1.00, 26/26).
  Correlation, no - `correlation_accuracy` requires `expected_groups`
  ground truth that no CLI invocation in this evaluation supplied.
- **Arm C added to the ablation table:** the table exists (section 3)
  with a Arm C column, but its cells all read `not measured` -
  `HELIX_LLM_API_KEY` is required and unset. The table records the gap
  honestly rather than closing it.

**What still blocks the exit criterion, and what unblocks each:**

1. `HELIX_LLM_API_KEY` must be set to a working Groq (or other
   OpenAI-compatible) endpoint to run arms B, C, and E at all. Nothing in
   this evaluation can substitute for that - no local model, no invented
   numbers, per the constraint this evaluation was run under.
2. Correlation accuracy needs `expected_groups` ground truth wired from
   the benchmark catalog (e.g., scenarios that inject a multi-source
   correlation chain and declare which event ids should land together)
   and fed into `event_layer_report`/`correlation_accuracy` from a
   harness run, not just from unit tests.
3. ~~MTTR on the event path~~ - fixed during this evaluation.
   `benchmark/harness.py::_first_observed_at` falls back to the earliest
   non-Helix `EventRecord.occurred_at` when an incident has no signal
   row, mirroring how `agent/graph.py::_signal_from_events` already falls
   back for diagnosis. Both paths now report a comparable MTTR.
4. `kind` and Elasticsearch would let the other 6 scenarios run,
   widening both the direct-path and event-path samples from 6/12 to
   12/12 scenarios - not required by the exit criterion's wording, but
   it would make every number in this document rest on the full
   catalogue instead of half of it.
