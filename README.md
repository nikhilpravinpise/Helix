# Helix

Helix is a modular agentic DevOps platform that starts with evidence-constrained incident recovery and grows into an event-driven engineering lifecycle system. Phase 1 proves recovery: an incident arrives, Helix collects real evidence, forms a hypothesis, picks a bounded action, validates it, executes or proposes it, and verifies recovery - useful and demoable on its own, with zero later-phase integrations required.

## Install

Requires [uv](https://docs.astral.sh/uv/). uv will fetch the pinned Python version (3.12) automatically.

```bash
uv sync
```

Copy `.env.example` to `.env` and fill in real values for local development. Never commit `.env`.

## Lint and test

```bash
make lint
make test
```

## Quickstart - the Phase 1 surface

Phase 2 (change-review lifecycle, Slack approvals/reviews, Jira/Linear/
Trello work items, Notion pages, Neo4j graph) is implemented - see
[plan.md](plan.md) Current Status, [docs/bring-up.md](docs/bring-up.md)
§6.5 for its variables, and [docs/runbooks/operator.md](docs/runbooks/operator.md)
for `helix review` / `helix sync` / `helix graph-sync`. Full bring-up
(clean-machine instructions, Terraform and script paths, hosts file,
teardown, memory notes) lives in [docs/bring-up.md](docs/bring-up.md).
The short version:

```bash
# Incident-state Postgres (host container, 127.0.0.1:5432)
bash scripts/db-up.sh

# Helix API + operator UI at http://127.0.0.1:8787/ui/
uv run helix serve

# CLI surface: intake a signal, list/diagnose/run incidents, approvals,
# provenance
uv run helix --help
```

The sandbox target - a kind cluster (`helix-sandbox`) running a
deliberately breakable `payment-api` behind ingress-nginx - is brought up
by Terraform (`infra/terraform/`, the documented path) or
`bash scripts/up.sh` (the escape hatch). With a hosts-file entry
(`127.0.0.1 payment-api.local`, `127.0.0.1 helix.local`) the services are
reachable at `http://payment-api.local` and `http://helix.local` - the
latter routes back out to the host-running `helix serve`.

Optional host stacks (memory-tight: do not run them all at once -
docs/bring-up.md §6): Elasticsearch/Kibana
(`infra/docker-compose.yml`), Jenkins (`infra/jenkins-compose.yml`,
including the SonarQube quality-gate stages), SonarQube
(`infra/sonarqube-compose.yml`).

```bash
# Controlled benchmark (1.11/2.11): fixture + change-flow scenarios by default;
# live scenarios need the sandbox stack. Reports: benchmark/reports/
bash scripts/benchmark.sh
```

## Status and verification

- **How much is done:** the Current Status block at the top of [plan.md](plan.md).
- **How to check it yourself:** [docs/verifying.md](docs/verifying.md), ordered by what each tier costs you.
