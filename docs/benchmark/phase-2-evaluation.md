# Phase 2 evaluation (subphases 2.7 and 2.11)

This reports what was measured for Phase 2 on 2026-10-02 and 2026-10-04,
on this machine. Every number below comes from a committed report. None is
rounded up or filled in, and every result known to be contaminated is
named as such.

- Model: `openai/gpt-oss-120b` on Groq, two API keys from separate
  organizations (failover on 429). No rate-limit failure in any run.
- Arms: A rules only, B single LLM call, C B plus retrieval, D C plus
  timeline and graph context, E full Helix with validation, verification
  and replanning.
- Work tracking and chat pinned to the in-memory fakes; owners and
  reviewers from `benchmark/service_registry.yaml` (alice, bob, carol).

| Report | What | Scenarios | Arms |
|---|---|---|---|
| `benchmark/reports/phase2-run1.json` | Full catalogue, kind + Elasticsearch up (2026-10-02) | 19 | A-E |
| `benchmark/reports/phase2-run2.json` | Stability run 2, graph off | 13 | A-E |
| `benchmark/reports/phase2-run3.json` | Stability run 3, graph off | 13 | A-E |
| `benchmark/reports/phase2-graph-on.json` | Graph on, graph as shipped | 13 | A, D |
| `benchmark/reports/phase2-graph-on-fixed.json` | Graph on, after the fix in d19ecd1 | 13 | A |

The 13 are every scenario that needs only Postgres: six CI and GitHub
recovery scenarios (`ci-dependency-failure`, `ci-docker-build-failure`,
`ci-test-regression`, `failed-check`, `rejected-pr`, `stale-branch`) and
seven change and cross-system scenarios (`org-*`, `xsys-*`). The other six
break the kind cluster and wait out a settle and a 60 s verification
window per scenario per arm; three runs of them would have taken most of a
night, so after run 1 they were skipped by decision.

---

## 1. Ablation table

Counts, not rates, so the denominator is visible. Runs 2 and 3 are shown
as `run 2 / run 3`.

### Recovery scenarios (the six CI and GitHub scenarios)

| Arm | Recovered | Root cause right | Valid action | Unsafe executions |
|---|---|---|---|---|
| A | 6 / 6 | 6 / 6 | 6 / 6 | 0 |
| B | 0 / 0 | 1 / 1 | 5 / 5 | 0 |
| C | 0 / 0 | 2 / 4 | 4 / 5 | 0 |
| D | 0 / 0 | 2 / 0 | 4 / 4 | 0 |
| E | 1 / 1 | 2 / 0 | 3 / 5 | 0 |

(each cell out of 6)

B, C and D cannot reach `recovered` by construction: their fused node has
no verification step, so they stop at `verifying`. Their recovery column
is structural, as in Phase 1.

### Change and cross-system scenarios (the seven `org-*` and `xsys-*`)

| Arm | Owner right | Reviewer right | Lifecycle consistent | Duplicate work items |
|---|---|---|---|---|
| A | 6/6, 6/6 | 4/4, 4/4 | 7/7, 7/7 | 0 |
| B | 6/6, 6/6 | 4/4, 4/4 | 5/7, 5/7 | 0 |
| C | 6/6, 6/6 | 4/4, 4/4 | 4/7, 5/7 | 0 |
| D | 6/6, 6/6 | 4/4, 4/4 | 4/7, 5/7 | 0 |
| E | 6/6, 6/6 | 4/4, 4/4 | 6/7, 6/7 | 0 |

Triage and review are plain code, so owner and reviewer accuracy do not
depend on the arm, and in runs 2 and 3 they no longer do (FOLLOWUPS #9,
where they did in run 1, is closed by this). The lifecycle misses are the
two cross-system scenarios that hand off to the recovery loop,
`xsys-pr-deploy-failure` and `xsys-runtime-missing-owner`, where B, C and
D stop at `verifying` or escalate, and E misses the first. Run 2's C and D
also miss `xsys-stale-work-item` because of the correlation defect in
section 5.
Decision evidence coverage was 1.00 on every arm in both runs.

### The six cluster scenarios (run 1 only)

| Arm | Recovered | Root cause right | Unsafe executions |
|---|---|---|---|
| A | 6 / 6 | 6 / 6 | 0 |
| B | 0 / 6 | 5 / 6 | 1 |
| C | 0 / 6 | 6 / 6 | 0 |
| D | 0 / 6 | 4 / 6 | 1 |
| E | 2 / 5 (1 skipped) | 4 / 5 | 0 |

## 2. Which components earned their place

- **Rules (arm A) beat every LLM arm on every recovery metric.** 12 of 12
  recovery scenarios recovered with the right root cause and a valid
  action, in every run. This is a closed-world benchmark whose faults the
  rule table was written for, so it bounds the LLM arms rather than
  beating them on open problems. That caveat is the same as Phase 1's.
- **Retrieval (C over B) earned its place.** Root cause on the six
  recovery scenarios went from 1 and 1 to 2 and 4, and 6 of 6 on the
  cluster scenarios against B's 5. This matches the Phase 1.5 finding.
- **Graph and timeline context (D over C) did not.** D's root cause was 2
  and 0 against C's 2 and 4, and 2 of 6 with the graph on. Adding the
  event timeline and graph neighbourhood to the prompt made diagnosis
  worse, not better.
- **Validation, verification and replanning (E) earned their place on
  safety, not accuracy.** E is the only LLM arm that can reach
  `recovered` (1 of 6 in each run, 2 of 5 on the cluster) and the only one
  that never executed an unsafe action in any run. Its root-cause accuracy
  (2 and 0) is no better than B's.
- **Run-to-run variance in the LLM arms is large.** Arm C's root cause
  moved from 2 to 4 of 6 and arm E's from 2 to 0 between two runs on the
  same code, model and scenarios. Two runs cannot separate C, D and E
  reliably; only A, and C over B, are clear.

## 3. Graph test (2.7): owner and reviewer accuracy with and without the graph

| Graph | Arm A owner | Arm A reviewer | Arm D owner | Arm D reviewer |
|---|---|---|---|---|
| Off (runs 2 and 3) | 6/6 | 4/4 | 6/6 | 4/4 |
| On, as shipped | 5/6 | **0/4** | 5/6 | **0/4** |
| On, after d19ecd1 | 6/6 | 4/4 | not rerun | not rerun |

The graph as shipped made both answers worse:

1. `reviewers_for` listed the service's owners before its reviewers, so
   the owner was asked to review every change.
2. Triage let the graph's service-level owner override the registry's
   path-level owner. A Dockerfile change, which the registry gives to bob,
   went to alice.
3. Neo4j returns neighbours in no promised order, and an unreachable graph
   raised out of triage and review instead of falling back.

All four are fixed in d19ecd1 with tests that failed first. On live Neo4j
the fixed graph matches graph-off on every scored field.

The plain conclusion: **the graph matches the registry but does not beat
it.** It is built from the same registry, so on this catalogue it can at
best agree. All four reviewer answers came from the graph; no owner answer
did, because every changed file has a path owner and the path answer now
stands. The graph would only add something with ownership facts the
registry lacks, such as review or commit history from GitHub, which is not
ingested yet.

## 4. Safety

- Runs 2 and 3: **zero unsafe executions on every arm.**
- Run 1: arms B and D each restarted the deployment on `oom-killed`, an
  action that scenario declares unsafe, and executed it. This is the same
  class as the Phase 1.5 safety finding: the fused arms have no
  validation step. Arms C and E did not.

## 5. Defects found by measuring, and the results they touched

Each was reproduced end to end, fixed with a test that failed first, and
is named here so no affected number is read as clean.

| Defect | Fix | Results affected |
|---|---|---|
| A deploy event addressed to one incident joined an older open incident on the same service, so the addressed one sat at `deploying` forever | 197f3e8 | run 1 B, C, D `xsys-stale-work-item`; run 2 C, D `xsys-stale-work-item` (lifecycle misses above) |
| Graph picked the owner as reviewer and overrode path owners | d19ecd1 | `phase2-graph-on.json` (the 0/4 is the finding, not noise) |
| The harness's pre-run close missed incidents left by event scenarios, so a later run inherited them | f90c7fa | run 3 arm A `xsys-pr-deploy-failure` (inherited an incident from a one-scenario reproduction; its score matched run 2 exactly); an earlier graph-on-fixed run, since replaced by a clean one |

## 6. Tolerance verdict: FAIL, and why that is not yet a judgement on Helix

`python -m benchmark.tolerance phase2-run2.json phase2-run3.json` reports
FAIL. Of its failures:

- **Two runs, not three.** Run 1 covers 19 scenarios, not the same 13, so
  it is not comparable.
- **Recovery band for B, C and D is `[0, 0]`.** True for Phase 1's
  recovery-only catalogue. Change scenarios recover the same way on every
  arm, so B, C and D now score 0.31 to 0.38 and fail a band that no longer
  describes them.
- **Approval gating counts reviews as approvals.** Every change scenario
  where a person reviews a PR fails "gate did not fire as declared". A PR
  review is not a safety approval.
- **Arm A root cause band is `[1, 1]`; A scored 7 of 8.** The miss is
  `xsys-runtime-missing-owner`, which A correctly escalates with "no rule
  matched".
- Some approval-gating failures are real: arm D three times chose a
  cluster write that needs approval (a rollback twice, a restart once) on
  GitHub scenarios whose right action does not.

The bands and the gating check were written for the 12 Phase 1 scenarios,
and no Phase 2 metric has a band yet (`docs/benchmark/tolerance.md`).
Setting them is a decision about what Phase 2 should be held to, recorded
as FOLLOWUPS #17 rather than guessed here.

## 7. What this does and does not establish

**Does:** the five-arm ablation populated on 13 scenarios twice and on all
19 once, on the pinned 120b model; owner and reviewer accuracy measured
with and without the graph; which components earned their place on this
catalogue; three Helix defects and one harness defect found and fixed.

**Does not:** three comparable runs, so no tolerance claim; the six
cluster scenarios more than once; arm D with the fixed graph (its owner
and reviewer answers cannot differ from arm A's, since triage and review
are arm-independent, but its diagnosis context was not rerun); any claim
beyond this catalogue, whose faults the rule table was written for.

## 8. Exit criterion status

- **2.7, "owner and reviewer accuracy is measured with and without the
  graph": met.** Measured, found worse, fixed, measured again.
- **2.11, "the full ablation table is populated across the full
  benchmark, and the analysis states plainly which components earned
  their place": partly met.** The analysis is above, and the table is
  populated across all 19 scenarios once (run 1). Stability across the
  full benchmark is not shown: the 13-scenario subset has two comparable
  runs, the cluster scenarios one, and the tolerance check fails for the
  reasons in section 6.
