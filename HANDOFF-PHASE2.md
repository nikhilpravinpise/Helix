# Handoff - Phase 2 (Full Helix)

**Date:** 2026-09-22
**Branch:** `main` - all Phase 2 work uncommitted in the working tree
**Tests:** 1233 unit, 50 integration (deselected by default, D9)
**Prior handoffs:** [HANDOFF-PHASE1.5.md](HANDOFF-PHASE1.5.md) |
[HANDOFF-PHASE1.md](HANDOFF-PHASE1.md)
**Verify any claim here yourself:** [docs/verifying.md](docs/verifying.md),
[docs/bring-up.md](docs/bring-up.md) (Phase 2 section)

---

## Status

**Phase 2 is implemented end to end: 2.1 through 2.11.**

| ID | Subphase | What landed |
|----|----------|-------------|
| 2.1 | Ops Agent consolidation | `agent/ops/` owns the recovery-loop node set; `build_graph` asks the boundary for arm E. New `Incident.kind` (`recovery`/`change`), `owner`, `duplicate_of_id`, review/triage/outbox/work-item/knowledge-page tables (migration `0011`). |
| 2.2 | Slack | `integrations/slack/`: web client + recording memory fake, HMAC signature verification, Block Kit notifications (status, approval buttons, review buttons), `POST /slack/interactions` routing to durable decisions, approval timeout fallback via the existing sweep. |
| 2.3 | Provider-neutral work mgmt | `integrations/work/contract.py` (`WorkManagementProvider`, `WorkItemRef`, capability maps, `WorkItemNotFoundError`), durable `integration_outbox` table, `integrations/lifecycle_sync.py` status→ops map, `integrations/outbox.py` drain with transient/permanent retry classes and dependency deferral, `helix sync`. |
| 2.4 | Notion | `integrations/notion/`: web client + memory fake, provenance page builder, `sync_page` outbox op, `knowledge_pages` mirror. |
| 2.5 | Triage Agent | `agent/agents/triage.py`: affected service, deterministic blast radius, ownership via graph → codeowners → registry → commit author (`"system"` never counts), duplicate detection by (service, branch). |
| 2.6 | Review Agent | `agent/agents/review.py`: reviewer resolution excluding the author and every prior rejector, durable `review_requests` rows, Slack review buttons, `helix review approve|reject`, API `/reviews/*`, timeout sweep → escalate. |
| 2.7 | Neo4j graph | `graph/`: schema, `MemoryGraphStore` + `Neo4jGraphStore` behind `GraphStore`, `sync_graph` re-derivation projection, `helix graph-sync`, queries (owners, reviewers, similar-incident resolvers, PRs-caused-by, dependency neighborhood). Gated by `HELIX_GRAPH_ENABLED`; everything works with it off. |
| 2.8 | Knowledge Agent / GraphRAG | `agent/knowledge_context.py` composes E6 retrieval + event timeline + graph neighborhood + resolved citations. Missing citations raise, never silently degrade. Arm D = fused call over the full composed context; arm E injects the context into diagnosis/planning when `graph_enabled`. |
| 2.9 | Lifecycle orchestration | Coordinator drives `received -> triaged -> owner_assigned -> review_pending -> approved/rejected -> fix_required -> deploying -> recovered`, with deploy-failure handoff into the Ops loop (`deploying -> diagnosing`). `integrations/reconcile.py` drift sweep (missing-remote recreate, status refresh, missing-terminal close) inside `helix sync`. `scripts/e2e_change_lifecycle.py` drives the whole thing through real `helix` subprocesses. |
| 2.10 | Linear + Trello | Providers behind the same contract with declared capability maps (Trello: no workflow state, no priority - declared, tested). |
| 2.11 | Evaluation | `benchmark/` gains `flow: change` scenarios (org-* and xsys-*), `benchmark/reviewer.py` scripted reviewer, `_drive_change` hop loop, `_settle_integrations` drain between hops, and new metrics: owner/reviewer accuracy, lifecycle consistency, duplicate work items, decision-evidence coverage. |

31 of 32 subphases across the whole plan. OPT-1 (multi-tenancy)
remains deliberately untouched.

---

## Architecture invariants (all verified by test)

- **Postgres is the source of truth.** Slack, Jira, Notion, Neo4j are
  projections. Every integration intent commits in the same transaction
  as the state transition that caused it (`integration_outbox` rows);
  delivery happens after, in the drain. A provider outage cannot lose a
  transition.
- **Outbox failure taxonomy.** Transient provider errors retry with
  attempts counted; permanent errors (unknown issue key on update, bad
  payload) fail terminally; a dependent op whose `create_work_item` is
  still pending defers without burning attempts, and fails terminally
  once the create is permanently dead. If the mirror says an item exists
  but the remote forgot it (drift or a fresh memory store), the drain
  recreates before dispatching the dependent op.
- **Slack is a door, not a store.** Interactions verify the signing
  secret and timestamp freshness, record the decision durably first,
  then resume the workflow best-effort. Stale/duplicate actions
  acknowledge with a note instead of corrupting state.
- **Reviews are durable rows** keyed per incident; re-review after a
  fix excludes every reviewer who already rejected.
- **The graph is derived.** `helix sync` re-derives it; a Neo4j outage
  degrades to a warning, never a failed sync.

## Defects found by this phase's verification and fixed

1. **Policy evidence-coherence (the 1.5 ablation's open defect).**
   `decide()` now takes the incident's canonical evidence types
   (signals classified through `events.classify`, plus event records'
   types). A runtime-mutating action - kubernetes or jenkins executor -
   proposed on source-control-only evidence escalates to
   REQUIRE_APPROVAL. The domain is the event TYPE, not the vendor: a
   `build.failed` via GitHub check-run is CI evidence and proceeds; a
   `pr.rejected` cannot authorize a cluster rollback.
2. **In-run replan for refused executions.** `executing -> failed` no
   longer wastes a whole recovery cycle on a candidate-level refusal:
   REFUSED replans in run for arms with a planner (E, A), bounded by
   the exclusion list as the budget. Transport ERRORs still go to
   `failed` and re-enter through `diagnosing` - replanning onto a dead
   transport would only burn candidates.
3. **Hallucinated directory trees.** `validate_fix_content` rejects any
   proposed file whose parent directory does not exist in the repo -
   the `src/main/java` signature arm E self-certified through in 1.5.
4. **Timestamp-precision ordering bug.** `Transition.created_at` /
   `EventRecord.created_at` used `func.now()` (second precision on
   SQLite) while event `occurred_at` carries microseconds - same-second
   comparisons went wrong both directions. Both columns now take an
   app-side microsecond-aware default (server default kept as the
   DDL fallback), and the "newer than" checks compare `created_at` to
   `created_at` - same clock, same precision, and the right semantic
   (when Helix learned of the event, not when the vendor claims it
   happened).
5. **Out-of-order change metadata.** `change_context` scans newest-first
   for the event carrying PR metadata rather than trusting the final
   non-Helix event - a late `build.failed` can no longer hide the author
   and slip a self-review through.
6. **Reopened PRs re-review.** GitHub's `reopened` action maps to
   `pr.opened`; fix detection at `fix_required` now watches both
   `pr.updated` and `pr.opened`, so a reopened PR carrying new code
   triggers re-review instead of parking forever.
7. **Orphaned `pr.updated`.** A PR update whose original change incident
   is terminal now opens a `change` incident rather than feeding a code
   event to the recovery loop.
8. **Dependency deferral semantics.** Outbox dependents of a pending
   create defer without burning attempts; dependents of a permanently
   failed create fail terminally; provider outages stay retryable.
9. **Memory-provider reconcile.** `helix sync` skips work-item
   reconciliation when the provider is `memory` - its "remote" is the
   local mirror, and a fresh process would falsely report total drift.
10. **Graph sync dead code.** `sync_graph` existed with no caller; it is
    wired into `helix sync` (and standalone `helix graph-sync`), fail-soft
    on Neo4j outage.

## What is verified

- **1233 unit tests green** (`uv run pytest`), 50 integration-marked
  tests deselected per D9 (they need live Postgres/kafka/kind/LLM).
- **Real subprocess E2E:** `scripts/e2e_change_lifecycle.py` drives
  pr.opened -> triage -> owner -> review_pending -> `helix review
  approve` -> approved -> deploying -> build.passed -> recovered through
  actual `helix` invocations, asserting triage/review rows, the work
  item, the knowledge page, and 21 delivered outbox ops including the
  drift repair.
- **Real benchmark run:** `benchmark.harness --scenario
  org-known-owner-change --arm A` against a migrated state store
  recovers the change incident and scores owner/reviewer/lifecycle/
  work-item metrics all correct.
- **Security surface:** slack signature verification rejects unsigned/
  stale/tampered payloads; review/approval API routes require the bearer
  token; unconfigured integrations skip instead of failing open.

## Known bounds (honest)

- Live-provider paths (Jira/Linear/Trello/Notion/Slack web clients) are
  exercised through the contract + memory fakes; no live credentials
  were available to this environment. The REST clients follow each
  vendor's documented API but live smoke is unverified.
- Neo4j store is contract-tested against a mocked driver;
  `HELIX_GRAPH_ENABLED=false` is the default and every arm works
  without it.
- Arm E's deeper hallucination finding (plausible content into real
  paths) is mitigated, not closed - `recovered` vs `measured` stay
  separate columns for exactly this reason.

## How to run it

```
# change lifecycle, end to end
uv run python scripts/e2e_change_lifecycle.py

# benchmark over the new scenario set (needs HELIX_DATABASE_URL)
uv run python -m benchmark.harness --scenario org-known-owner-change --arm A \
    --database-url sqlite:///path/to/state.db

# integration drain + drift reconcile + graph re-derivation
helix sync
```
