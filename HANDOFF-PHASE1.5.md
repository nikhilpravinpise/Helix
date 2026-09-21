# Handoff - Phase 1.5 (Event-Driven Helix)

**Date:** 2026-09-20
**Branch:** `phase-1.5-event-layer` (25 commits, not pushed, no PR opened)
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
| E7 | Phase 1.5 evaluation | **Measured once - stability unmet** |

**E7's remaining half.** A key was supplied on 2026-09-20 and all four
arms ran for the first time. The exit criterion's ablation clause is met;
the 1.11 stability clause is not.

**One valid four-arm run** (arm A 1.00 with zero spread, so the control
is clean):

| arm | RCA | action valid | measured | unsafe |
|---|---|---|---|---|
| A rules | 1.00 | 1.00 | 1.00 | 0.00 |
| B single LLM | 0.50 | 0.83 | 0.17 | 0.00 |
| C LLM + retrieval | 0.83 | 0.83 | 0.67 | 0.00 |
| E full graph | 0.33 | 0.83 | **0.00** | 0.00 |

**The finding.** Arm E reached `recovered` on four of six scenarios and
measured correct on zero. It opened fix PRs against paths like
`sandbox/payment-api/src/main/java/...` - a Java tree in a Python project.
Helix's own verification passed them; only the harness's independent
measurement caught that the change was fictional. Every one of those
writes was approved by the policy gate as `allow_with_checks`, and they
were harmless solely because fixture mode records intended writes rather
than opening real PRs (D10). With `HELIX_GITHUB_TOKEN` set, arm E would
have opened four real PRs editing files that do not exist.

**What retrieval bought**, visible in the paths: arm B wrote an invented
`config/payment.yaml`; arm C wrote `pyproject.toml`, `Dockerfile`,
`tests/test_main.py` - all real. RCA 0.50 -> 0.83, measured 0.17 -> 0.67.
Retrieval's contribution here was grounding the model in paths that
exist, because the incidents it cites reference real ones.

**Stability is unmet.** Two attempts at three consecutive runs were
discarded: the first to cross-invocation contamination (now fixed), the
second to Groq's daily token cap (199,672 of 200,000 on
`openai/gpt-oss-120b`). Arm A held 1.00 with zero spread throughout both,
which is what proves the harness was sound and the LLM arms hit an
external wall. Those zeros are not recorded as arm performance.

Phase 2 was deliberately not started. 2.1 needs no LLM, but it
restructures the agents into the Phase 2 shape, and the entire purpose of
the ablation is determining which components earned their place.

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

1. **Three-run stability, not the ablation.** The ablation is measured;
   the 1.11 tolerance criterion needs three consecutive runs in band and
   the daily token quota ran out. Cheapest route is `openai/gpt-oss-20b`
   for stability runs - variance is what that criterion measures, and a
   smaller model measures variance fine even at lower absolute scores.
2. **Arm E hallucinates file paths** and its own verification does not
   catch it. This is the most important open finding in the project and
   is not a benchmark defect - it is a real behaviour of the full graph.
3. **A provider 404 and a rate limit used to be indistinguishable from a
   model with nothing to say.** Fixed - the cause is carried into the
   transition - but the benchmark report still surfaces workflow errors
   as an empty `errors` list, so the reason lives only in the database.
4. **Correlation ground truth is two groups.** Widening it needs fixture
   captures pairing more vendor sources against one documented incident.
5. **`tests/integrations/test_live_integration.py` fails rather than
   skips** when live infrastructure is absent, unlike the 8 tests that
   skip cleanly for the same reason. Deliberately left alone - a hard
   failure is arguably correct for a tier you only run on purpose.
6. **Nothing pushed.** No PR. That is a human decision.
7. **The DJS23OLOE501 cloud requirement is unmet**, deferred by decision
   (D23) rather than dropped, and now stubbed. `infra/gcp/README.md`
   scopes the full kind-to-GKE delta; `scripts/gcp-up.sh` is the entry
   point and refuses to run, printing the prerequisites. Nothing in the
   repo depends on GCP, so this is additive work rather than a migration.
   Two things to get right when it is built: pin the cluster version, or
   a GKE auto-upgrade mid-series turns the three-run tolerance criterion
   into a measurement of GKE; and keep the benchmark local, because it
   rebuilds cluster state on every run.

---

## Next actions

1. **Rotate the Groq key.** A failing test printed it in plaintext before
   the repr guard landed. The guard is committed; the exposed key is not
   revoked.
2. **Three stability runs** once the daily quota resets, then
   `make benchmark-tolerance REPORTS="r1 r2 r3"`. Consider
   `openai/gpt-oss-20b` to stay inside the cap.
3. **Human:** decide whether these 25 commits go out as one PR or are
   split per subphase, matching the one-PR-per-subphase pattern Phase 1
   used.
3. **Then:** Phase 2 from 2.1, with the ablation available to say which
   components earned their place before they are restructured.
