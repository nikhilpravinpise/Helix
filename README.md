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
