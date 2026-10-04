# Why Helix testing takes so long

Short version: the unit tests are fast (1355 tests in about a minute). The slow
part is the benchmark, which runs Helix for real, on every scenario, under every
version of the agent, several times over. That is what Phase 2 has to show, and
it can't be skipped.

## What one benchmark run actually does

- **13 scenarios x 5 arms = 65 full Helix runs.** The arms are the versions we
  compare: A (rules only), B (one LLM call), C (+ past incidents), D (+ graph),
  E (the full agent). Comparing them is the whole point of the Phase 2 results.
- Each of the 65 is a real run, not a mock. Helix gets the event, triages it,
  picks an owner and reviewer, plans a fix, checks it, and the result is scored.
- Arms B to E call a real LLM (Groq) on every step, so they wait on the API and
  are limited by its free-tier rate limits. A previous attempt died partway
  through because it hit the daily token cap.
- **One run of the 13 scenarios takes about 16 minutes** (measured tonight:
  23:07 to 23:22).

## Why it has to run more than once

- LLM answers vary from run to run. One good run proves nothing, so the plan
  requires **three runs** and a check that the numbers stay within tolerance.
- The graph test needs extra runs on top: owner and reviewer accuracy measured
  with the graph on and with it off.

## The parts we already cut to save time

- 6 of the 19 scenarios break a real Kubernetes cluster and wait for Helix to
  fix it. Each one resets the cluster, injects the fault, waits up to 2 minutes
  for the rollout to settle, then watches the fix hold for another minute. Times
  5 arms times 3 runs, that would have run until about 2 AM, so those are
  **skipped for now**.
- The laptop has only about 4-5 GB of free memory, so the cluster,
  Elasticsearch, Neo4j and Jenkins can't all run at once. They get started one
  at a time, only when a test needs them.

## Running it for real finds bugs the tests didn't

All of these were found by running the system, not by the green test suite.
Each one was fixed, with a test added:

- Unit tests were creating real Jira tickets (about 107, since deleted).
- A CI failure could trigger a rollback on the cluster.
- Helix said "recovered" after opening a fix PR while the pod was still crashing.
- Two different PRs got merged into one incident, so one was never reviewed.
- Clicking Approve in Slack did nothing visible.
- Rollback went back one version blindly instead of to the last working one.
- The benchmark skipped every LLM arm because of where the API keys were set.
- Tonight: a "deploy passed" event went to the wrong open incident, so the
  right one waited at "deploying" forever.

Shipping without these runs would have meant shipping those bugs.

## Where it stands now

- Run 1 and run 2: done.
- Run 3: in progress.
- Left: the graph on/off test, arm D with the graph, then the write-up
  (`docs/benchmark/phase-2-evaluation.md`).
