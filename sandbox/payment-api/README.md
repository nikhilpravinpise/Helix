# payment-api

A small FastAPI service used as the sandbox target for Helix's fault
injection catalogue (Helix CONTEXT.md, subphase 1.2). It is a demo
application, not part of Helix itself - it exists so Helix has something to
deliberately break and then observe, diagnose, and remediate.

It is a separate uv project with its own `pyproject.toml`, `uv.lock`, and
dependencies, on purpose: some faults (e.g. a deliberate test regression) are
meant to fail this service's own pipeline without touching Helix's `make
test`.

## Running locally

Requires the required config below - the process fails fast at startup if
either is missing:

```bash
cd sandbox/payment-api
uv sync
PAYMENT_API_API_KEY=dev-key \
PAYMENT_API_DOWNSTREAM_URL=http://localhost:9999/pay \
uv run uvicorn payment_api.main:app --reload
```

## Running tests

```bash
cd sandbox/payment-api
uv run pytest
```

## Fault catalogue coverage

Each feature exists to serve a specific fault in the 1.2 catalogue:

| Feature | Fault(s) served |
|---|---|
| `GET /health` - cheap liveness check, no dependency checks | CrashLoopBackOff, failed rollout |
| `GET /ready` - checks the downstream dependency, 503 if unreachable | dependency failure, failed rollout |
| `POST /payments` - calls the downstream dependency | dependency failure |
| Required `PAYMENT_API_API_KEY` / `PAYMENT_API_DOWNSTREAM_URL`, no defaults, fail-fast startup | missing Secret, config mismatch, CrashLoopBackOff |
| `PAYMENT_API_BALLAST_MB` memory ballast (off by default) | OOMKilled |
| Test suite (`tests/`) | test regression |
| `Dockerfile` | Docker build failure |

Bad deployment config, CrashLoopBackOff/OOMKilled reproduction, and failed
rollout are triggered against this image's Kubernetes deployment manifests,
which are a later 1.2 deliverable - not part of the application code itself.
