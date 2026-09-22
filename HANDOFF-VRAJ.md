# Handoff - To Vraj: Final Stage

**Date:** 2026-09-23
**Branch:** `main` - everything merged through PR #21, tree clean
**Tests:** 1241 unit green (`uv run pytest`, re-run today), 50 integration
deselected by default (D9), lint clean, CI green on `main`
**Prior handoffs:** [HANDOFF-PHASE2.md](HANDOFF-PHASE2.md) |
[HANDOFF-PHASE1.5.md](HANDOFF-PHASE1.5.md) |
[HANDOFF-PHASE1.md](HANDOFF-PHASE1.md)

You own Helix from here. The mission, in order: **verify every claim in
every handoff yourself, audit the code beyond what the suite covers,
fill the environment, then finish what is left.** The plan is 31/32
subphases done. Your job is the last subphase, the unmeasured
measurements, the unverified live paths, and the two external targets.

Do not trust this document. Do not trust the earlier handoffs either -
several of their "verified" claims were only true on the day they were
written. `docs/verifying.md` is the verification procedure; run it
before you change anything and again after.

---

## Step 0 - Verify before touching

```bash
make lint && make test          # expect: clean, 1241 passed / 50 deselected
git log --oneline -10           # confirm you are on the merge of PR #21
git status                      # expect: clean
```

Then read, in this order: [CONTEXT.md](CONTEXT.md) (the contract),
[plan.md](plan.md) (status block + D1-D23 decisions), the three prior
handoffs, `docs/verifying.md`, `docs/bring-up.md`. The plan's status
block and the Phase 2 handoff disagree slightly on test counts - both
are stale; 1241 is today's number.

## Step 1 - Audit

The suite is green. That is weak evidence and this repo has the scars to
prove it (see the Phase 1.5 defect ledger - arm C never executed a node
for three subphases with a green suite). Audit these specifically, in
this order:

1. **Safety surfaces.** `agent/policy.py` evidence-coherence check,
   `agent/actions/catalogue.py` reversibility declarations, the approval
   gate, Slack signature verification, API bearer-token routes. Any hole
   here is a sev-1 finding. Try to break them - write the adversarial
   test, do not just read the code.
2. **Outbox and reconciliation.** `integrations/outbox.py`,
   `integrations/reconcile.py`, `integrations/lifecycle_sync.py`.
   Transient/permanent retry classes, dependency deferral, drift repair.
   These are the newest and least-live-tested code in the repo.
3. **Arm E hallucination bound.** `agent/validation/fix_pr.py` rejects
   nonexistent parent directories, but plausible-wrong content into
   *real* paths is still open. Decide whether another mitigation is
   warranted or whether `recovered`/`measured` separation is the
   documented answer. Record the decision either way.
4. **The change lifecycle end to end.** Run
   `uv run python scripts/e2e_change_lifecycle.py` and read what it
   actually asserts versus what HANDOFF-PHASE2.md claims it asserts.
5. **Stale docs.** HANDOFF-PHASE2.md's header says Phase 2 is
   uncommitted - it is merged. Fix stale claims as you find them; docs
   are part of the product.

## Step 2 - Fill the environment

`.env` is partially filled (LLM, GitHub, DB, Kafka, ES, kube context
set). Everything below is still empty. `.env.example` documents each
one. Fill all of them - do not leave a provider unwired and call the
phase done.

| Variable(s) | Unblocks | Get it from |
|---|---|---|
| `HELIX_JENKINS_TOKEN` | Jenkins evidence + writes | Jenkins UI after `scripts/jenkins-up.sh` |
| `HELIX_NEO4J_PASSWORD` | Real `Neo4jGraphStore` | your Neo4j container/instance |
| `HELIX_SLACK_BOT_TOKEN`, `HELIX_SLACK_SIGNING_SECRET` | Live Slack notify/approve/interactions | Slack app config |
| `HELIX_JIRA_BASE_URL`, `HELIX_JIRA_EMAIL`, `HELIX_JIRA_API_TOKEN` | Live Jira provider | Atlassian |
| `HELIX_LINEAR_API_KEY`, `HELIX_LINEAR_TEAM_ID` | Live Linear provider | Linear settings |
| `HELIX_TRELLO_API_KEY`, `HELIX_TRELLO_API_TOKEN`, `HELIX_TRELLO_BOARD_ID` | Live Trello provider | Trello app key |
| `HELIX_NOTION_API_KEY`, `HELIX_NOTION_PARENT_PAGE_ID` | Live Notion pages | Notion integration |
| `HELIX_VERIFY_BASE_URL` | Functional leg through ingress | `http://payment-api.local` once hosts entry exists |
| `HELIX_INTAKE_TOKEN` | Intake auth on non-local surface | generate |

**Security:** `.env` stays out of git. The Groq key in git history was
printed in plaintext by a failing test during Phase 1.5 - rotate it
(`console.groq.com`) before reusing it, then confirm the new key works.

## Step 3 - Bring the world up

Per `docs/verifying.md` tier 3 and `docs/bring-up.md`:

```bash
make db-up && make db-migrate    # Postgres
make kafka-up                    # broker + topics
bash scripts/up.sh               # kind + images + ingress + manifests
bash scripts/logging-up.sh       # Elasticsearch + Kibana
bash scripts/jenkins-up.sh       # Jenkins + SonarQube
```

RAM note from verifying.md is real: ~11GB is already held on this
machine and the stacks do not all fit at once. Bring up what a given run
needs; `make db-down`/`make kafka-down` when done (volumes persist).

Then run the integration tier: `make test-integration`. The
live-integration test *fails rather than skips* without infra - that is
deliberate; now that you have infra, it should pass. If it does not,
that is a defect, fix it.

## Step 4 - Finish the measurements

This is the bulk of what "remaining" actually means. The code for all of
it exists; none of it has been run to completion.

1. **Full ablation, all five arms, all 19 scenarios.**
   `bash scripts/benchmark.sh --arm A --arm B --arm C --arm D --arm E`.
   Arm D has never been benchmarked. Read arm A first - if it is not
   1.00, the harness is contaminated, discard the run.
2. **Three-run stability on the pinned model** (`openai/gpt-oss-120b`),
   inside `docs/benchmark/tolerance.md` bands, checked with
   `make benchmark-tolerance REPORTS="r1.json r2.json r3.json"`. The
   prior stability claim is on `gpt-oss-20b` - the 120b daily token cap
   killed the third run. Plan around the cap.
3. **Write `docs/benchmark/phase-2-evaluation.md`.** 2.11's exit
   criterion is the populated table plus a written analysis stating
   which components earned their place - including wherever more
   architecture did not help. The Phase 1.5 evaluation doc is the model.
4. **Live provider smoke.** With real credentials, create/transition/
   comment/close one work item on each of Jira, Linear, and Trello; post
   one real Slack approval flow; write one real Notion page; run
   `helix graph-sync` against real Neo4j. The provider study (2.10) is
   only proven on memory fakes until this happens.
5. **Live `open_fix_pr`.** `HELIX_GITHUB_TOKEN` is set - run one
   scenario that ends in a real PR and confirm the `fix_pr_exists` leg.
   Note the Phase 1.5 warning: arm E's hallucinated paths were harmless
   only because writes were faked. Watch what it actually opens.

## Step 5 - The two external targets

- **OPT-1 (PyCaret MLOps)** - the only unstarted subphase. Plan text is
  in `plan.md` under "Optional Extension": PyCaret trained on incident/
  event history predicting one narrow target (severity or recurrence),
  trained and evaluated through Jenkins, output advisory only. Lab unit
  10 rides on it.
- **GCP (D23).** Required by DJS23OLOE501. `infra/gcp/README.md` has the
  complete kind-to-GKE delta and the two things to get right (pin the
  cluster version; keep the benchmark local). Prerequisites are
  interactive (`gcloud auth`, billing) - get them, then do the one-off
  GKE deployment of `payment-api` the README prescribes.

## Step 6 - Housekeeping

- PRs #17 and #18 are open Helix-authored remediation PRs from Phase 1
  live runs - review and merge or close them deliberately.
- Local branches `phase-2-full-helix`, `fix/verification-rollout-settle`
  are fully merged - delete when convenient.
- Update `plan.md`'s status block and this file as reality moves.
- Unanswered question to escalate: does DJS23OLOE501 have a hard
  deadline? It decides whether anything in the scope-compression order
  gets cut.

## Rules that bind you (non-negotiable, from CONTEXT.md/CLAUDE.md)

- A failing test means the code is wrong. Never weaken a test to pass.
- Reproduce bugs end to end before fixing.
- No em dash characters anywhere. Plain `-`.
- No co-author lines in commit messages.
- `CHANGELOG.md` and generated files: never hand-edit.
- Secrets never in git, never in logs, never in LLM prompts.
- High-risk actions stay behind the approval gate. No arbitrary shell
  for the LLM, ever.
- Recovered vs measured stay separate columns. Report `measured`.

## Done means

- Full five-arm ablation table populated across all 19 scenarios on the
  pinned model, three consecutive runs in band, analysis written.
- Every provider verified live, not just on fakes.
- OPT-1 exit criterion met.
- GCP deployed per `infra/gcp/README.md`.
- The integration tier green, the unit tier still green, lint clean.
- `plan.md` reads 32/32 and every "known bounds" section either closed
  or carried forward honestly.
