# Handoff - Phase 1.5 (Event-Driven Helix)

**Date:** 2026-09-20
**Branch:** `phase-1.5-event-layer` (18 commits, not pushed, no PR opened)
**Plan:** [plan.md](plan.md) E1-E7 | prior handoff: [HANDOFF-PHASE1.md](HANDOFF-PHASE1.md)

---

## Where things stand

E1 through E7 are implemented. Every subphase exit criterion is met
except one, and that one cannot be met on this machine.

| ID | Subphase | Status |
|----|----------|--------|
| E1 | Canonical EngineeringEvent model | Done |
| E2 | Kafka backbone | Done |
| E3 | Event adapters, idempotent ids | Done |
| E4 | Correlation and consumption | Done |
| E5 | Helix event emission, history, replay | Done |
| E6 | Retrieval over history, ablation arm C | Done |
| E7 | Phase 1.5 evaluation | **Partial - see below** |

**E7's open half, and the only thing blocking Phase 2.** plan.md E7
requires "ablation arm C measured against arms A, B, and E". Arms B, C
and E all reason through an LLM, and `HELIX_LLM_API_KEY` is unset. Three
of E7's four deliverables are done and measured; the ablation table's
arm B/C/E cells are marked `not measured` with the reason, never
estimated or left blank.

That single gap blocks E7's exit criterion, which blocks Phase 1.5's exit
gate, which is **Phase 2's entry gate**. One free Groq key fills the
whole table in a single run.

Phase 2 was deliberately not started. 2.1 needs no LLM, but it
restructures the agents into the Phase 2 shape, and the entire purpose of
the pending ablation is determining which components earned their place.

---

## What was built

```
events/
  schemas.py      canonical envelope, taxonomy, derive_event_id
  classify.py     Phase 1 signal -> event type
  adapters/       kubernetes, jenkins, github, elk (+ _common redaction)
  broker.py       Kafka producer/consumer, DLQ
  correlation.py  token extraction, CORRELATION_WINDOW
  ingest.py       ingest_event, run_consumer
  lifecycle.py    Helix's own five lifecycle events
  history.py      query surface, replay, IncidentProjection
  retrieval.py    lexical IDF retrieval + citations
  emit.py         outbox drain
  metrics.py      E7's four measurements
benchmark/
  compare.py      direct-path vs event-path
  correlation/    ground-truth groups from captured fixtures
  tolerance.py    numeric bands (arms B/C/E provisional)
```

CLI gained `helix consume`, `helix emit`, `helix reclaim`. Migrations
0008 (event records, correlation tokens), 0009 (published_at), 0010
(stale_reason). Decisions D15-D22 recorded in plan.md.

---

## Defects found, all fixed and regression-tested

Listed because the pattern matters more than the individual bugs: **every
one was found by running something, not by reading it.** The suite was
green throughout.

| Defect | How it was found |
|---|---|
| Benchmark printed "no successful executions row" under every passing check | Reading the committed report |
| `--mode fixture` raised KeyError, killing the run | Reading the mode branch |
| `crashloop` and `oom-killed` shared a dedup fingerprint, so an OOM inherited a crashloop's remediation | Capturing baseline fingerprints |
| Graph could not read an event-sourced incident at all | Running the benchmark through the event path |
| MTTR silently null on the event path while the comparator said NO REGRESSION | Reading the event-path report directly |
| **Arm C had never executed a single node** since E6 created it | Running the LLM arms for the first time |
| Hard-killed runs left incidents permanently unrecoverable | A subprocess timeout during those runs |
| Benchmark arms inherited each other's incidents | Running several arms in one invocation |
| Kubernetes Warning about a pod used a different token than the pod itself | Measuring correlation accuracy |
| Correlation window bounded only the future, merging faults 22 hours apart | The same measurement |

Two of these were mine and shipped behind tests that asserted the
author's own edit rather than the behaviour: arm C's
`_ARM_NODES["C"] == _ARM_NODES["B"]`, and the benchmark's executed-action
check. A third pattern appeared twice in subagent work - writing a test
that **asserts a defect as expected behaviour**, which records a bug as a
requirement.

---

## Measured results

- **Direct vs event path:** no regression across all five gated metrics
  on arm A; MTTR 1.4845 direct against 1.4752 event.
- **Correlation accuracy:** 1.00 over 2 groups. Two is narrow, and the
  document says so. The rate was the least useful thing it produced - it
  exposed the two correlation defects above.
- **Replay correctness:** 1.00 over 26 incidents, 0 not-replayable.
- **Duplicate handling:** 1.00 over 114 events and **zero redeliveries**,
  which proves nothing; the metric reports `redeliveries` beside the rate
  precisely so that is visible.
- **Event latency:** median 0.0112s over 13 non-Helix records - an
  in-process call, not a Kafka round trip.
- **Arm A:** 1.00 on every metric, which is a deterministic lookup table
  returning its own answer key and supports no claim about the
  architecture.

---

## Known gaps

1. **The ablation.** Arms B, C and E unmeasured. The blocker.
2. **Ollama proved the arms execute**, which genuinely closes E6's "arm C
   runs end to end", but a 3B local model measures model size rather than
   architecture and its scores are deliberately kept out of the table.
3. **Arm E produced one unsafe execution** on the local model - the first
   real non-zero value the D2 unsafe-action measurement has ever had, as
   a rules table cannot choose wrongly. Worth revisiting once a
   representative model runs.
4. **Correlation ground truth is two groups.** Widening it needs fixture
   captures pairing more vendor sources against one documented incident.
5. **`tests/integrations/test_live_integration.py` fails rather than
   skips** when live infrastructure is absent, unlike the 8 tests that
   skip cleanly for the same reason. Deliberately left alone - a hard
   failure is arguably correct for a tier you only run on purpose.
6. **Nothing pushed.** No PR. That is a human decision.
7. **The DJS23OLOE501 cloud requirement is unmet**, deferred by decision
   (D23) rather than dropped. Helix runs on kind only. Nothing in the repo
   depends on GCP, so this is additive work, not a migration - and the
   benchmark should stay local regardless, because a GKE auto-upgrade
   mid-series would turn the three-run tolerance criterion into a
   measurement of GKE.

---

## Next actions

1. **Human:** drop a free Groq key into `.env` as `HELIX_LLM_API_KEY`.
   Then `bash scripts/benchmark.sh --arm A --arm B --arm C --arm E`
   fills the ablation table, and E7, Phase 1.5's exit gate and Phase 2's
   entry gate all unblock together.
2. **Human:** decide whether these 18 commits go out as one PR or are
   split per subphase, matching the one-PR-per-subphase pattern Phase 1
   used.
3. **Then:** Phase 2 from 2.1, with the ablation available to say which
   components earned their place before they are restructured.
