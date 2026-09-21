# Handoff - Phase 1.5 (Event-Driven Helix)

**Date:** 2026-09-21
**Branch:** `phase-1.5-event-layer` - 29 commits, **not pushed, no PR**
**Tests:** 898 unit, 50 integration (deselected by default, D9)
**Prior handoffs:** [HANDOFF-PHASE1.md](HANDOFF-PHASE1.md) |
[HANDOFF-1.2.md](HANDOFF-1.2.md)
**Verify any claim here yourself:** [docs/verifying.md](docs/verifying.md)

---

## Status

**Phase 1.5 is complete. E1 through E7, all exit criteria met.**
Phase 2's entry gate is open.

| ID | Subphase | Exit criterion |
|----|----------|---------------|
| E1 | Canonical EngineeringEvent model | Met - benchmark unchanged |
| E2 | Kafka backbone | Met - delivery, restart durability, DLQ |
| E3 | Event adapters, idempotent ids | Met - all vendors, no shape escapes |
| E4 | Correlation and consumption | Met - four sources, replay is a no-op |
| E5 | Emission, history, replay | Met - projection matches live state |
| E6 | Retrieval, ablation arm C | Met - arm C runs end to end |
| E7 | Evaluation | Met - arm C measured against A, B, E |

20 of 32 subphases across the whole plan. Phase 2 (2.1-2.11) is
deliberately not started.

---

## What the ablation concluded

Three consecutive runs, `openai/gpt-oss-20b`, 6 of 12 scenarios (no kind
cluster, no Elasticsearch). Median, spread in brackets. Evidence:
`benchmark/reports/e7-stability-{1,2,3}.json`.

| metric | A rules | B single LLM | C LLM + RAG | E full graph |
|---|---|---|---|---|
| RCA accuracy | **1.00** [0.00] | 0.33 [0.17] | **0.83** [0.17] | 0.33 [0.17] |
| action validity | **1.00** [0.00] | 0.67 [0.00] | **1.00** [0.17] | 0.33 [0.33] |
| measured correct | **1.00** [0.00] | 0.17 [0.00] | **0.67** [0.17] | 0.00 [0.17] |
| unsafe action rate | 0.00 | **0.33** | 0.00 | 0.00 |

CONTEXT.md section 19 asks which components earned their place.

**Retrieval did.** Arm C beats arm B on every reasoning metric, across
six runs on two models. The mechanism is visible in what each arm wrote:
arm B opened a PR against an invented `config/payment.yaml`, arm C wrote
`pyproject.toml`, `Dockerfile`, `tests/test_main.py` - all real.
Retrieval grounded the model in paths that exist.

**The full graph did not.** Arm E is the worst LLM arm and the least
stable at this model scale.

**Arm A's 1.00 is not a system result.** It is a lookup table keyed on
fault signature returning its own answer key. Its zero spread measures
the 1.1 determinism policy, and it is the control: if it ever reads
anything other than 1.00, the harness is contaminated.

---

## Two open defects, both found by running the system

### 1. The policy gate allows incoherent actions (safety)

Arm B chose `rollback_deployment` - a Kubernetes write against
`payment-api` - in response to a **rejected pull request** and a
**diverged branch**. Both scenarios declare that action explicitly
unsafe. It executed, in 2 of 3 runs.

```
action          : rollback_deployment
policy_decision : allow_with_checks
approval_id     : None
result          : success
```

`agent/policy.py` judges an action's risk tier **in isolation** and never
asks whether the action is coherent with the evidence that produced it.
Nothing connects "this incident came from GitHub" to "this action mutates
Kubernetes."

Harmless only because fixture mode records intended writes rather than
performing them (D10). **With a live context in
`HELIX_ALLOWED_KUBE_CONTEXTS`, this is a real rollback of a running
service triggered by a code-review comment.**

Arms A, C and E never did it. Arm B is the only arm with no validation
stage - the ablation demonstrating what validation is for.

`benchmark/tolerance.py` fails its verdict on this, correctly.

### 2. Arm E hallucinates file paths and self-certifies

Arm E reached `recovered` on four of six scenarios while measuring
**zero**. It opened fix PRs against
`sandbox/payment-api/src/main/java/...` - a Java source tree in a Python
project. Helix's own verification passed them.

Caught only by the harness's independent `measured` check. This is why
`recovered` (Helix's verdict) and `measured` (the harness checking) are
separate columns, and why `recovery_success` requires both.

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
  emit.py         transactional outbox drain
  metrics.py      E7's four measurements
benchmark/
  compare.py      direct-path vs event-path
  correlation/    ground-truth groups from captured fixtures
  tolerance.py    numeric bands, executable
```

CLI: `helix consume`, `helix emit`, `helix reclaim`.
Migrations 0008-0010. Decisions D15-D23 in plan.md.

---

## Defect ledger

Every one was found by **running** something, never by reading it or by
the suite going green. The suite was green throughout.

| Defect | Found by |
|---|---|
| Benchmark printed "no successful executions row" under every passing check | Reading the committed report |
| `--mode fixture` raised KeyError, killing the run | Reading the mode branch |
| `crashloop`/`oom-killed` shared a dedup fingerprint | Capturing baseline fingerprints |
| Graph could not read an event-sourced incident | Running the event-path benchmark |
| MTTR silently null while the comparator said NO REGRESSION | Reading the event-path report |
| **Arm C had never executed a single node** since E6 created it | Running the LLM arms |
| Hard-killed runs left incidents permanently unrecoverable | A timeout during those runs |
| Benchmark arms inherited each other's incidents | Running several arms at once |
| Arm A inherited a *previous invocation's* incident | Arm A's control number moving |
| K8s Warning used a different token than the pod itself | Measuring correlation accuracy |
| Correlation window bounded only the future, merging faults 22h apart | The same measurement |
| LLM key in `.env` invisible to the harness | A configured key still skipping |
| Pinned model decommissioned by the provider | The arms failing with no reason |
| **A failing test printed a live API key in plaintext** | That test failing |
| A provider 404 and a rate limit looked like an empty answer | Debugging the above twice |

Three of these were mine, shipped behind tests that asserted the author's
own edit rather than the behaviour - arm C's
`_ARM_NODES["C"] == _ARM_NODES["B"]` is the clearest example. A separate
pattern appeared twice in subagent output: a test asserting a defect as
expected behaviour, which records a bug as a requirement.

---

## Known gaps

1. **Policy gate and arm E hallucination** - the two defects above.
2. **Rotate the Groq key.** It was printed in plaintext by a failing test
   before the `repr=False` guard landed. The guard is committed; the
   exposed key is not revoked.
3. **Three-run stability is on `gpt-oss-20b`, not `gpt-oss-120b`.** The
   120b daily token cap (200k) stopped run 3 of that attempt after two
   valid runs. Stability measures variance, which holds on either model,
   but the headline ablation and the stability runs are different models
   and their absolute scores are not mixed.
4. **6 of 12 scenarios.** No kind cluster, no Elasticsearch.
   `scripts/up.sh` and `scripts/logging-up.sh` unlock the rest; watch RAM.
5. **Four Phase 1 gaps** from HANDOFF-PHASE1.md still open: no
   `HELIX_GITHUB_TOKEN`, Ansible syntax-only, Jenkins/SonarQube down,
   `executing -> failed` does not replan.
6. **Cloud (D23).** DJS23OLOE501 requires it; Helix is kind-only.
   Stubbed: `infra/gcp/README.md`, `scripts/gcp-up.sh` (refuses to run).
7. **`tests/integrations/test_live_integration.py` fails rather than
   skips** without live infra. Left deliberately.
8. **Nothing pushed.** 29 commits, no PR.

---

## Next actions

1. **Rotate the Groq key.**
2. **Decide the PR shape** - one PR for the branch, or split per subphase
   to match Phase 1's pattern. This is the only thing blocking the work
   going outward.
3. **Fix the policy gate** so an action must be coherent with the
   evidence behind it, not merely low-risk in isolation. Arguably 2.1
   territory, since that subphase consolidates the agent boundary anyway.
4. **Then Phase 2 from 2.1** - a pure structural move whose exit
   criterion is the Phase 1 benchmark passing unchanged, now with the
   ablation available to say which components earned their place.
