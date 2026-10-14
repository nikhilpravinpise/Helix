# Helix

Helix is an agentic DevOps system that handles two kinds of engineering work
on its own, and asks a human at the points that matter:

- **Recovery.** A pod crashes, a build fails, a check goes red. Helix
  collects real evidence, forms a hypothesis, picks a bounded action from a
  fixed catalogue, checks it against a safety policy, runs it or asks for
  approval, and verifies the system actually recovered.
- **Change review.** A pull request opens. Helix triages it, picks the owner
  and a reviewer from the service registry, follows review, rework and
  deploy, and hands a failed deploy to the recovery loop.

Postgres is the source of truth. Slack, Jira, Notion and a Neo4j graph are
projections of it, fed through a transactional outbox.

**Status: Phase 1, Phase 1.5, Phase 2 and the optional OPT-1 are done**
(32 of 32 subphases). OPT-1 is an advisory PyCaret model, see
[mlops/README.md](mlops/README.md). See the Current Status block in
[plan.md](plan.md) and the evaluation in
[docs/benchmark/phase-2-evaluation.md](docs/benchmark/phase-2-evaluation.md).

## How it works

```
GitHub / Jenkins / Kubernetes / Elasticsearch
        |  helix watch, webhooks, Jenkins post-build
        v
   Kafka events  ->  helix consume --follow --run
        |                (correlate into incidents)
        v
   Coordinator graph (LangGraph)
     recovery: diagnose -> plan -> validate -> policy -> execute -> verify
     change:   triage -> owner -> review -> deploy
        |
        |  high-risk or out-of-bounds?  ->  a human approves (Slack, CLI, UI)
        v
   Postgres (incidents, evidence, decisions, approvals, audit)
        |  transactional outbox, helix sync
        v
   Slack threads, Jira tickets, Notion pages, Neo4j graph
```

The LLM never gets a shell. It chooses from a catalogue of typed actions
(restart, rollback, scale, reapply a manifest, rerun a pipeline, open a fix
PR), and every write passes a policy check twice: once when the coordinator
routes it, and again inside the executor.

## Human in the loop

Helix acts alone only on low and medium risk work in non-production, inside
the namespaces its registered services run in. Everything else waits for a
person, and only the right person may decide:

| Decision | Who may make it |
|---|---|
| Approve or reject an action | An owner of the service (registry `default_owner` / `owners`) |
| Approve or request changes on a PR | Only the reviewer Helix asked |
| Either, on their own change | Never the change's author |

What always needs approval: high-risk actions (Secret changes, destructive
operations), any write in production or an unknown environment, a cluster
write outside a registered namespace, and a runtime write proposed on
evidence from another domain (for example, a rollback because a PR was
rejected). Helix opens fix PRs but never merges them. Every approved action
records who approved it.

Identities are registry handles (GitHub logins). Slack clicks map through
`people.<handle>.slack`; the CLI uses `HELIX_OPERATOR` or `--actor`. A
refused Slack click tells the person why.

## Install

Requires [uv](https://docs.astral.sh/uv/), which fetches the pinned Python
(3.12).

```bash
uv sync
cp .env.example .env   # fill in what you use; never commit .env
```

## Run it

```bash
# Incident-state Postgres (127.0.0.1:5432) and its schema
make db-up && make db-migrate

# One incident by hand
uv run helix intake --file signal.json   # prints INC-id; see docs/runbooks/operator.md
uv run helix run <INC-id>
uv run helix approve <INC-id>            # as HELIX_OPERATOR

# Running on its own (needs Kafka: make kafka-up)
uv run helix consume --follow --run      # turn events into incidents and drive them
uv run helix watch --source kubernetes   # failing pods -> events
uv run helix watch --source github       # new and updated PRs -> events
uv run helix slack-socket                # Slack buttons, no public URL needed
uv run helix sync                        # deliver Slack/Jira/Notion updates

uv run helix --help                      # everything else
```

The sandbox target is a kind cluster (`helix-sandbox`) running a deliberately
breakable `payment-api` behind ingress-nginx: `bash scripts/up.sh`, or
Terraform in `infra/terraform/`. Optional host stacks (Elasticsearch,
Jenkins, Neo4j, SonarQube) live in `infra/`. The dev machine has little free
memory, so start only what a task needs. Full bring-up:
[docs/bring-up.md](docs/bring-up.md). A guided walkthrough: [DEMO.md](DEMO.md).

## Configure

- **`.env`**: credentials and settings, all `HELIX_*`. `.env.example` lists
  every variable. Integrations left unset are skipped, never crashed on.
- **`config/service_registry.yaml`**: the live team. For each service: its
  owners, reviewers, path owners (CODEOWNERS-style), dependencies and
  Kubernetes namespace; and for each person, a display name and Slack
  member id. Select another file with `HELIX_SERVICE_REGISTRY`.
- **`benchmark/service_registry.yaml`**: the fixed cast (alice, bob, carol)
  the benchmark and tests use, kept apart from the live team.

## Test and measure

```bash
make lint
make test                  # unit tier, ~1 minute, needs nothing
bash scripts/benchmark.sh  # the benchmark; see docs/verifying.md
```

The benchmark runs failure scenarios against five ablation arms - A rules,
B one LLM call, C plus retrieval, D plus graph context, E full Helix - and
scores root cause, action validity, recovery, safety, owner and reviewer
accuracy, and lifecycle consistency. Phase 2's three final runs pass the
tolerance check with zero unsafe actions on every arm. A full run takes
time; [WHY_TESTING_TAKES_TIME.md](WHY_TESTING_TAKES_TIME.md) explains why.

## Documents

| Document | What it is |
|---|---|
| [plan.md](plan.md) | Phases, decisions (D1-D26), and the Current Status block |
| [CONTEXT.md](CONTEXT.md) | Architecture and design rules |
| [docs/verifying.md](docs/verifying.md) | How to check Helix yourself, by cost |
| [docs/bring-up.md](docs/bring-up.md) | Clean-machine setup |
| [docs/runbooks/operator.md](docs/runbooks/operator.md) | Following an incident as a human |
| [docs/architecture/](docs/architecture/) | Safety policy, state machine, agent graph, verification |
| [docs/benchmark/](docs/benchmark/) | Methodology, tolerance bands, Phase 1.5 and Phase 2 evaluations |
| [docs/audit-2026-10-05.md](docs/audit-2026-10-05.md) | Codebase audit: what was found and fixed |
| [FOLLOWUPS.md](FOLLOWUPS.md) | Known issues, parked with enough detail to pick up |
| [future_plans.md](future_plans.md) | Beyond the plan: Helix as a product, dashboard redesign |
