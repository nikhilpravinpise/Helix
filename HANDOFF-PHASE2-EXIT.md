# Handoff - Phase 2 exited, and what comes next

**Date:** 2026-10-09
**Branch:** `main`, in sync with `origin/main` (`VrajVed/Helix`) at `5ac9c3c`
**Tests:** see [docs/verifying.md](docs/verifying.md); all pass in a clean
checkout (one timing test can fail with a local `.env`, see FOLLOWUPS #20)
**Prior handoffs:** [HANDOFF-PHASE2.md](HANDOFF-PHASE2.md) |
[HANDOFF-VRAJ.md](HANDOFF-VRAJ.md) | [HANDOFF-FIXES.md](HANDOFF-FIXES.md)

---

## Status

**Phase 1, Phase 1.5 and Phase 2 are done.** 31 of 32 subphases; only the
optional OPT-1 (PyCaret) is left in plan.md. The Phase 2 exit criteria are
measured, not just built:

- **2.7 met.** Owner and reviewer accuracy measured with and without the
  Neo4j graph. As shipped the graph was worse (reviewer 0/4); fixed, and it
  now matches the registry.
- **2.11 met.** Five-arm ablation over all 19 scenarios once and the 13
  Postgres-only scenarios across three final runs; tolerance check
  **PASS**. Write-up: [docs/benchmark/phase-2-evaluation.md](docs/benchmark/phase-2-evaluation.md)
  section 0.

## What changed since HANDOFF-PHASE2

**Human-in-the-loop rules** (decided by Vraj, enforced in
`state/deciders.py` and `agent/policy.py`):

| Decision | Who may make it |
|---|---|
| Approve or reject an action | An owner of the service (registry `default_owner` / `owners`) |
| Approve or request changes on a PR | Only the reviewer Helix asked |
| Either, on their own change | Never the change's author |

Plus: a cluster write outside a namespace a registered service runs in
needs approval; every approved write records its approver; a refused
Slack click gets a private "Not recorded" reply.

**Tolerance check** (`benchmark/tolerance.py`, FOLLOWUPS #17 closed):
recovery-flow and change-flow scenarios are checked against their own
metrics; approvals counted, not PR reviews; bands for the five Phase 2
metrics. An LLM arm asking for approval it did not need is reported, not
gated (arm A, the control, stays gated; unsafe actions gated on every arm).

**Codebase audit** ([docs/audit-2026-10-05.md](docs/audit-2026-10-05.md)):
21 findings, 17 fixed, each with a test that failed first. Among them:
an LLM key blacklisted for an hour after one 429, Slack text injection,
a PR swallowed by a crash incident, a step timeout looping forever, a
Secret fix PR skipping approval via `./` in its path, outbox messages
sent twice, watcher events lost on a Kafka error, arm A targeting the
wrong service, and substring root-cause scoring.

**Live integration test** (2026-10-06): an approval clicked in Slack was
recorded as VrajVed, resumed the run and moved the Jira ticket to In
Progress; a rejection escalated it and moved the ticket to Blocked. Two
bugs it found are fixed: Slack was silently faked whenever evidence came
from fixtures, and Jira "Blocked" read back as "open".

## Live setup

- **LLM:** Groq `openai/gpt-oss-120b`, two keys from separate orgs
  (`HELIX_LLM_API_KEY` + `HELIX_LLM_API_KEYS`), failover on 429/401. A
  full five-arm run can hit the daily cap; rerun the affected arm the next
  day.
- **Integrations:** Jira Cloud, Slack via Socket Mode (`helix slack-socket`,
  no public URL), Neo4j, Jenkins, GitHub polling. Notion and Linear are
  wired but not set up.
- **Identity:** `HELIX_OPERATOR=VrajVed` in `.env`; Vraj's Slack member id
  is in `config/service_registry.yaml`. **Nikhil has no Slack id there**,
  so his Slack clicks are refused until it is added.
- **Fix PRs** go to the throwaway repo `VrajVed/helix-sandbox`, never merged
  by Helix.
- **Benchmark registry:** `benchmark/service_registry.yaml` (alice, bob,
  carol), separate from the live team.

## Open work

| Item | Size | Notes |
|---|---|---|
| **GCP deployment (D23)** | 1 session | The course (DJS23OLOE501) requires a cloud deploy. Needs Vraj's `gcloud` login and billing. `infra/gcp/README.md` scopes it. |
| **FOLLOWUPS #20** | small | Unit tests read the developer's `.env`; make the unit tier ignore it. |
| **B3** (FOLLOWUPS #18) | small | `xsys-runtime-missing-owner` replays payment-api evidence for a billing-worker event. |
| **E4** (FOLLOWUPS #18) | medium | Events carry ingest time, not vendor time; needs a benchmark check before changing correlation. |
| **E1b** (FOLLOWUPS #18) | needs a decision | Should a crash during an open PR join its change incident? Suggested: only while deploying. |
| **I5** (FOLLOWUPS #18) | small | A Notion page deleted in the UI reads as archived, not missing. Not reproduced. |
| **Dashboard redesign** | 1-2 sessions | Current UI is unpresentable. Scoped in [future_plans.md](future_plans.md). |
| OPT-1 PyCaret | optional | Course unit, outside the Helix thesis. |
| GitHub App (#15), ngrok webhooks (#16), product plan | deferred | By decision. |

Housekeeping: close PR #21; Jira KAN-122 and the two Slack test threads
in #helix-incidents are test leftovers.

## Working rules that matter here

- No em dashes, no co-author lines, no "Generated with Claude Code".
- Commit straight to `main`; Vraj pushes and opens the PR into
  `nikhilpravinpise/Helix`.
- A failing test means the code is wrong. Reproduce a bug before fixing it.
- Never print `.env` values; the dev box has ~5 GB free RAM, so start only
  the stacks a task needs.
- Before trusting a benchmark number, check arm A (the control) and that
  no incident was inherited from an earlier run.
- Subagent output needs checking: in the last batch, three of four Haiku
  fixes needed correcting (one would have silenced every GitHub PR, one
  loosened a fix-PR safety rule).
