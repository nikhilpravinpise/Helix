# Handoff - Phase 1 (Evidence-Constrained Incident Recovery)

**Date:** 2026-09-16
**Branch:** `main`
**Plan:** [phase1-remaining-plan.md](phase1-remaining-plan.md) | architecture
contract: [CONTEXT.md](CONTEXT.md) | prior handoff: [HANDOFF-1.2.md](HANDOFF-1.2.md)

---

## Where things stand

Phase 1 is complete and verified end-to-end on live infrastructure. All
remaining subphases from the plan (P0, 1.3-1.13) were implemented serially,
each independently tested and merged via its own PR. The benchmark's arm A
(rules-only, no LLM) recovered every scenario in the catalogue, including the
five live Kubernetes fault scenarios on the kind sandbox, and the
`open_fix_pr` path was proven against real GitHub (a real PR opened, read
back, and verified - see Verification).

| ID | Subphase | PR | Status |
|----|----------|-----|--------|
| P0 | Pre-work: provider swap to Groq, pytest markers, packaging, CLI stub | #4 | Done |
| 1.3 | Evidence layer: 5 capability integrations, fixtures, redaction | #5 | Done |
| 1.4 | Incident state + intake: Postgres, state machine, dedup, API+CLI | #6 | Done |
| 1.5 | Coordinator + diagnosis: LangGraph, checkpointer, bounded evidence loop | #7 | Done |
| 1.6 | Recovery planner: action catalogue, deterministic ranker, scenarios | #8 | Done |
| 1.7 | Validation: per-action strategies, honest NOT_VALIDATABLE | #9 | Done |
| 1.8 | Policy + execution: verdicts, write transports, approval gate, audit | #10 | Done |
| 1.9 | Verification + provenance: stability window, functional leg, export | #11 | Done |
| 1.10 | Unhappy paths: compensation, retry budget, idempotency, timeouts | #12 | Done |
| 1.11 | Benchmark: 12 scenarios, harness, scripted approver, scoring | #13 | Done |
| 1.12 | Operator UI: incident list/detail, provenance timeline, approvals | #14 | Done |
| 1.13 | Infrastructure: kind config, Terraform, Ansible, Jenkins+Sonar gate, ingress | #15 | Done |
| - | Post-1.13 live hardening: rollout-settle verification, dry-run parity | #16 (branch `fix/verification-rollout-settle`) | Done |

---

## Architecture as built

```
signal -> intake (dedup fingerprint) -> incident row
  -> coordinator -> diagnose (evidence loop, hypothesis)
  -> plan (catalogue candidates, deterministic rank)
  -> validate (per-action strategy) -> policy -> approval gate
  -> execute (write transports, idempotent) -> verify (settle + window)
  -> recovered | replan | compensate | escalate
```

- `agent/graph.py` - LangGraph composition + Postgres checkpointer
  (`thread_id = incident_id`), `default_deps()` wires live vs fixture.
- `agent/agents/*` - coordinator owns every transition; diagnosis,
  planner, validation, execution, verification, compensation nodes.
- `agent/rules.py` - arm-A deterministic diagnosis/planning tables.
- `agent/verification/` - per-action desired-state legs; rollout settle
  phase precedes the stability window for deployment-mutating actions.
- `integrations/` - evidence + write capability protocols; vendor SDKs
  never reach agent reasoning.
- `state/` - models, machine, intake/dedup, provenance assembly.
- `helix/` - config, OpenAI-compatible LLM client, FastAPI app + `/ui`,
  CLI (`intake`, `run`, `approvals`, `provenance`, `serve`).
- `benchmark/` - scenario YAMLs, harness (drives the real CLI), scoring.

## Decisions worth knowing

- **LLM provider**: Groq via OpenAI-compatible API (D14 in
  phase1-remaining-plan.md). `HELIX_LLM_BASE_URL`/`MODEL`/`API_KEY` are
  provider-neutral - Ollama works for dev runs. `temperature=0` + `seed`
  are sent when the provider accepts them (determinism policy is now
  literally implementable).
- **Verification settles before it samples.** Deployment-mutating
  actions wait for `rollout_complete` convergence (budget
  `HELIX_VERIFY_SETTLE`, default 120s) before the stability window opens;
  pod turnover during the intended rollout is not flapping. Inside the
  window, a previously-Ready pod vanishing counts only when no pod is
  Ready that sample, and pods mid-deletion are excluded.
- **Dry-run predicts the executor.** `dry_run_apply` shells out to
  `kubectl apply -f - --dry-run=server` - client-side apply, the same
  code path the write transport uses. A pure SSA dry-run was rejecting
  valid manifests on field-ownership conflicts the real apply never hits.
- **Reversibility is three-way** (`stateless|compensable|irreversible`),
  not a bool - it drives verify-fail routing in 1.10.
- **Dedup attaches only to OPEN incidents**; a signal matching a terminal
  incident starts a new one. Killed runs can leave stale open incidents -
  close them via `state.machine.transition(..., ESCALATED, cause=...)`
  before re-running a scenario (done twice during this pass).

## Verification

Re-run independently, not accepted on report:

- `uv run ruff check .` / `ruff format --check .` - clean.
- `uv run pytest` - 611 passed, 35 deselected (integration tier).
- `uv run pytest -m integration` - 25 passed; live k8s prechecks,
  Selenium functional leg through real port-forward, Postgres intake
  round-trips all green. Remaining failures are environmental only
  (Jenkins/GitHub services not up).
- Jenkins pipeline ran live end-to-end in 1.13: install -> tests ->
  coverage -> Sonar analysis -> quality gate -> image build -> kind
  deploy -> `/ready` 200.
- Ingress live: `payment-api.local/health` and `helix.local/ui` -> 200.
- Benchmark arm A, `mode=auto`, all 12 scenarios - see
  `benchmark/reports/latest.md` / `latest.json` for the authoritative
  numbers (final grid run regenerated after the verification fixes).
- `open_fix_pr` live proof: real PR opened on `nikhilpravinpise/Helix`
  (branch `helix/INC-*`), verified by the `fix_pr_exists` leg, incident
  recovered. Left open for review.

## Live-defect ledger (all fixed, all regression-tested)

1. `dry_run_apply` namespaced every request `default` - grouped by
   declared namespace first, then replaced wholesale by the kubectl-CLI
   parity implementation above.
2. `PodHealthCheck` read intentional rollout turnover as flapping - the
   settle phase + replacement-scoped disappearance rule.
3. Terminating pods counted as workload health - excluded via
   `deletionTimestamp` in `normalize_pod`.
4. Fault scripts timed out on cold clusters - assertion windows raised
   (90-180s) and documented.
5. Secret-caused crashloops couldn't recover inside the window because
   kubelet backoff outlived it - `restart_deployment` tabled as the
   post-fix step for `crashloop`/`missing-secret`.
6. Harness subprocess timeouts (300s) shorter than a verify cycle -
   `helix run` 900s, fault scripts 420s.
7. Selenium e2e asserted `session_id is None` post-quit (version drift) -
   now asserts the chromedriver process actually exited.
8. Missing `python3` prerequisite - documented in `docs/bring-up.md`
   including the Windows `python.exe` -> `python3.exe` alias.

## Environment notes / known gaps

- `HELIX_LLM_API_KEY` (Groq) is still unset on this machine - arms B/E
  and live-LLM tests were honestly skipped, not faked. Set it and re-run
  `helix-benchmark --arm B --arm E` when a key is available.
- Live GitHub writes need `HELIX_GITHUB_TOKEN` (`gh auth token` works);
  without it `open_fix_pr` errors at execution and the incident goes to
  `failed` (execution errors do not replan - see below).
- Ansible validated by syntax only - no native Windows control node.
- Jenkins + SonarQube were verified live in 1.13 then torn down;
  `scripts/jenkins-up.sh` brings them back.
- Watch item: `executing -> failed` on execution transport error does not
  replan within the same run. For a compensable action that never wrote,
  a replan hop would likely recover - worth revisiting if real runs hit
  it (the GitHub-token case was the only observed trigger).
- Dedup is same-source only (documented limitation from 1.4).

## What Phase 1.5 adds next (per CONTEXT.md)

Canonical event model on Kafka, event correlation, replay, and durable
event history - built on top of this incident pipeline, not in place of
it. Phase 2 then adds Slack/Jira/Notion coordination and Neo4j/GraphRAG.
Nothing from those phases was pulled forward.
