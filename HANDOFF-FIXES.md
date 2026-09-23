# Handoff - Phase 2 reconciliation

**Date:** 2026-09-23
**Branch:** `fix/phase2-reconciliation` (5 commits off `main` at `1e824a2`)
**Tests:** 1252 unit passing, 50 integration deselected (D9), lint clean
**Prior handoffs:** [HANDOFF-VRAJ.md](HANDOFF-VRAJ.md) |
[HANDOFF-PHASE2.md](HANDOFF-PHASE2.md) | [HANDOFF-PHASE1.5.md](HANDOFF-PHASE1.5.md)

## Why this branch exists

Phase 2 landed and is real. An audit of the two new handoff documents
against the code found the architecture matches CONTEXT.md closely and
almost every structural claim checks out. What did not hold up was the
gap between "implemented" and "exited", plus five concrete defects.

This branch closes the defects and makes the documents honest. It does
**not** do the remaining measurement work - that is still open and is
the real Phase 2 debt.

## What changed

| Commit | What |
|---|---|
| `f92b756` | Recovered 8 orphaned tests |
| `7d4616d` | Repointed `latest.*` at a run that measured something |
| `ae684a9` | Made the Phase 2 handoff's verification claims true |
| `86ab1ac` | Recorded the missing Phase 2 decisions, split implemented from exited |
| `b8fcbee` | Gave arm D a tolerance band |

### 1. Eight tests existed only on a local branch

`c572892` ("Test the two fixes that had no tests") sat unpushed and
unmerged on local `phase-1.5-event-layer`. It covers `_first_observed_at`
(the MTTR clock) and `_close_preexisting_incidents` (the fix preventing
the contamination that invalidated an ablation run). **A fresh clone of
`main` did not have them.** Cherry-picked; the suite went 1241 -> 1249.

### 2. `latest.md` was a broken run, committed as evidence

`benchmark/reports/latest.*` is the committed current-evidence pointer.
It held the 2026-09-20 run where every LLM arm reads 0.00 on every
metric - the `model_not_found` failure recorded as D14a, not a result.
`make benchmark-tolerance` defaults to reading it.

Regenerated from `e7-stability-3.json` via the harness's own
`write_report` rather than hand-editing generated markdown. Arm A reads
1.00 in both files, so the control confirms the old one was broken
rather than worse. Now reads **A 1.00, C 0.83, B 0.33, E 0.17** on RCA
accuracy - the Phase 1.5 finding as measured.

### 3. Arm D could never pass the tolerance gate

`BANDS` had A, B, C, E - no D. An arm with no bands produces an
`unknown_arm` finding at severity `incomplete`, and `ok` is computed
over gated **and** incomplete findings. So any run including arm D
reported FAIL regardless of results, which made `HANDOFF-VRAJ.md` step 4
("run all five arms, then check tolerance") impossible as written.

Reproduced before fixing. Arm D mirrors arm C exactly - D is C with the
composed knowledge context of 2.8, same fused node, same absent verify
stage - so `recovery_success` is a structural `Band(0.0, 0.0)`.

### 4. Three claims in `HANDOFF-PHASE2.md` did not survive checking

- Header said Phase 2 was uncommitted. It was merged via PR #21.
- `WorkItemNotFoundError` **does not exist anywhere in the repo**. The
  contract signals not-found by returning `None`.
- The script was claimed to assert the knowledge page and 21 outbox ops
  "including the drift repair". It **printed** both and asserted
  neither. Rather than weaken the claim, `scripts/e2e_change_lifecycle.py`
  now asserts them. The drift-repair half is simply wrong and now says
  so: the script runs `HELIX_WORK_PROVIDER=memory`, and `helix sync`
  deliberately skips work-item reconciliation for the memory provider.

### 5. Phase 2 had one decision record, and it was about GCP

Phase 1 and 1.5 earned D1-D22. D8 was also stale - it read
`HELIX_ARM=A|B|E` while the code types five arms. That is not cosmetic:
the same omission is why arm C reached three subphases without executing
a node.

Added **D24** (capability contract + transactional outbox), **D25**
(Slack is a door, not a store), **D26** (the graph is re-derived and
gated off by default). D8 now names all five arms.

## The honest status

`plan.md` now separates implemented from exited: **11/11 implemented,
9/11 exited.**

**2.7** requires owner and reviewer accuracy measured with and without
the graph. **2.11** requires the populated five-arm ablation plus written
analysis. Neither has an artifact. `docs/benchmark/phase-2-evaluation.md`
does not exist, and **arm D has still never been benchmarked.**

## What is left

1. Five-arm ablation, all 19 scenarios, pinned `openai/gpt-oss-120b`.
   **Read arm A first - if it is not 1.00, discard the run.**
2. Three runs, then `make benchmark-tolerance` (now possible).
3. Write `docs/benchmark/phase-2-evaluation.md` on the
   `phase-1.5-evaluation.md` model.
4. 2.7's with/without-graph comparison.

Plan around, do not discover: the 120b daily token cap killed the third
stability run last time (the prior claim is on `gpt-oss-20b`), and six
of twelve Phase 1 scenarios skip without the sandbox stack, so every
published rate so far is out of six.

See [FOLLOWUPS.md](FOLLOWUPS.md) for issues found and deliberately
parked.

## Verify this branch yourself

```bash
make lint && make test                          # clean, 1252 passed / 50 deselected
uv run python scripts/e2e_change_lifecycle.py   # PASS, with the new assertions
git log --oneline main..HEAD                    # the 5 commits above
```
