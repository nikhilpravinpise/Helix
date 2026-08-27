# Handoff - Subphase 1.2 (Sandbox Target Environment)

**Date:** 2026-08-25
**Branch:** `main`, working tree
**Commits made:** none. Everything below is uncommitted and awaiting review.

---

## Where things stand

Subphase 1.1 is complete and verified. Subphase 1.2 is **one deliverable in of seven**, and the remaining six are blocked on a single decision: where the sandbox cluster lives.

| ID | Subphase | Status | Note |
|----|----------|--------|------|
| 1.1 | Repository and Toolchain Foundation | Done | Lint, tests, CI, config, logging, observability, LLM interface |
| 1.2 | Sandbox Target Environment | 1 of 7 | `payment-api` built and verified, rest blocked |
| 1.3 | Evidence Layer | Not started | Depends on 1.2 for recorded fixtures |

---

## Subphase 1.2 deliverable ledger

| Deliverable | Status | Blocked by |
|---|---|---|
| `payment-api` service, Dockerfile, test suite | **Done** | Target-agnostic, so the cluster decision cannot invalidate it |
| Kubernetes manifests (deployment, service, config, secret, ingress) | Blocked | Ingress and image registry references differ between kind and GKE |
| Jenkins in Docker outside the cluster (D1) | Blocked | Observer plane placement undecided |
| Jenkins pipeline: build, test, containerize, deploy | Blocked | Deploy step targets a cluster that does not exist yet |
| ELK receiving application and cluster logs | Blocked | Placement, and whether Logstash stays in scope |
| Fault injection catalogue (9 faults) | Partly open | The 2 CI-side faults could start now; the 7 cluster-side faults cannot |
| `scripts/` bring-up and teardown | Blocked | Nothing to bring up yet |

---

## What was built

`sandbox/payment-api/` as a **separate uv project**, not part of the Helix package.

```
sandbox/payment-api/
├── payment_api/
│   ├── __init__.py
│   ├── config.py        # pydantic-settings, PAYMENT_API_ prefix
│   └── main.py          # FastAPI app, fail-fast startup
├── tests/
│   ├── conftest.py      # seeds required env before main is imported
│   ├── test_config.py   # 4 tests
│   └── test_main.py     # 9 tests
├── Dockerfile           # multi-stage uv, python:3.12-slim, non-root
├── .dockerignore
├── pyproject.toml
├── uv.lock
└── README.md
```

**Endpoints**

- `GET /health` - cheap liveness, no dependency checks. Serves CrashLoopBackOff and failed-rollout scenarios.
- `GET /ready` - readiness, returns 503 when the downstream dependency is unreachable. Serves dependency-failure and failed-rollout.
- `POST /payments` - pydantic-validated body, calls the downstream dependency, returns 201 with a payment record, 502 on downstream failure.

**Configuration** (env, prefix `PAYMENT_API_`)

| Variable | Required | Purpose |
|---|---|---|
| `PAYMENT_API_API_KEY` | yes, no default | Missing-Secret fault |
| `PAYMENT_API_DOWNSTREAM_URL` | yes, no default | Config-mismatch and dependency-failure faults |
| `PAYMENT_API_BALLAST_MB` | no, default 0 | OOMKilled fault against a low memory limit |

The two required values deliberately have **no defaults**. A default would silently defeat three faults in the catalogue. Missing or whitespace-only values cause a logged failure and a non-zero exit at startup.

Every feature of the service traces to a specific entry in the fault catalogue. Nothing speculative was added.

### Root change

Two lines in `/pyproject.toml`, both surgical:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]        # added

[tool.ruff]
extend-exclude = ["sandbox"] # added
```

This is load-bearing, not tidiness. The catalogue includes a deliberate test regression, and that must fail the `payment-api` pipeline **without** breaking Helix's own `make test`. Sharing a project would make those two indistinguishable.

---

## Verification

Every check below was re-run directly, not taken from the implementing agent's report.

| Check | Result |
|---|---|
| `uv run ruff check .` / `format --check` (sandbox) | All checks passed, 7 files formatted |
| `uv run pytest` (sandbox) | 13 passed in 0.24s |
| `docker build` | OK |
| Container `GET /health` | `200 {"status":"ok"}` |
| Missing `api_key` | exit 1 |
| Missing `downstream_url` | exit 1 |
| Whitespace-only `api_key` | exit 1 |
| Runtime user | `uid=1000(appuser)` |
| Secrets in image layers | none |
| Root `make lint` / `make test` | pass, still exactly 7 tests |
| Em dash scan over `sandbox/` | clean |

All three fail-fast paths were tested rather than the one originally specified. That non-zero exit is what produces the CrashLoopBackOff and missing-Secret faults later; had it exited zero, two catalogue entries would have been silently unreproducible.

### Reproducing

```sh
cd sandbox/payment-api
uv run ruff check . && uv run ruff format --check .
uv run pytest
docker build -t payment-api:test .
docker run --rm -p 8000:8000 \
  -e PAYMENT_API_API_KEY=k \
  -e PAYMENT_API_DOWNSTREAM_URL=http://example.invalid \
  payment-api:test
```

---

## Blocked on a human decision

**The cluster target is the critical path.** GCP was described as probable, not settled.

Host toolchain as of this handoff:

| Tool | State |
|---|---|
| `kubectl`, `kind`, `docker`, `terraform` | installed and working |
| `gcloud`, `helm`, `minikube` | **not installed** |

There is no GCP project and no billing account, and `gcloud` authentication is interactive so it cannot be done by an agent.

**To go the GCP route**, three things are needed from a human:

1. Install the SDK, then run `gcloud auth login` and `gcloud auth application-default login`.
2. Create or name a project and attach a billing account.
3. Name a region and zone, so a single zonal cluster can be pinned.

**To stay local**, nothing is needed. Work can resume immediately at zero cost.

---

## Open decisions

| Decision | Why it matters | Recommendation |
|---|---|---|
| Retire assumption 1 in plan.md | plan.md records a local cluster as an assumption whose stated rationale is reproducibility. Moving to cloud contradicts it. | Retire it deliberately and record a **D3**, rather than letting it drift |
| Split 1.2 into 1.2a / 1.2b | Largest subphase in Phase 1, bundles five unrelated pieces of infrastructure with no checkpoint between them | 1.2a service and cluster, 1.2b CI, logging, faults |
| Observer plane placement | ELK was going to run beside the thing being deliberately broken. A log store should not die with the system it observes. | Jenkins and ELK together, off-cluster, per D1's intent |
| Drop Logstash | 1.3 needs only bounded log queries by service, time window and level | Filebeat straight to Elasticsearch. Syllabus mapping still reads as ELK |
| Pin the cluster version (GCP only) | If GKE auto-upgrades mid-benchmark, the 1.11 tolerance band measures GKE rather than Helix | Release channel `None`, autoscaling off, fixed node count |
| Namespace-scoped reset (GCP only) | Locally, reset could mean deleting the cluster: free and instant. On GKE a recreate is minutes and real money, repeated across every benchmark run. | Treat the cluster as long-lived infrastructure |
| Wire the API key through | `api_key` is required but nothing reads it, so it currently reads as dead config | Send it as an `Authorization` header on the downstream call. 1.3's exit criterion needs a real secret travelling in evidence to prove redaction against |
| Do not build dual targets | Supporting both kind and GCP is speculative flexibility | 1.3's recorded fixtures are already the fast iteration path from 1.4 onward |

---

## Next actions, in order

1. **Human:** settle the cluster target. Everything else in 1.2 waits on this and nothing else does.
2. **Human:** if GCP, install the SDK and authenticate. If local, say so and work resumes immediately.
3. **Human:** decide whether plan.md gets amended with D3 and the 1.2a / 1.2b split before implementation continues.
4. **Either:** wire `api_key` into the downstream request as an `Authorization` header. One line, unblocks a real redaction test in 1.3.
5. **Next session:** Kubernetes manifests for `payment-api`, written against the chosen target.
6. **Next session:** fault injection catalogue. Each fault needs a bounded wait-for-signal with a machine-checkable assertion, so 1.2 does not pass on a good day and then flake inside the 1.11 benchmark. CrashLoopBackOff and OOMKilled are the timing-dependent ones.
7. **Next session:** capture recorded fixtures while each fault is being triggered and verified, rather than standing the whole environment back up during 1.3. plan.md 1.3 requires these but 1.2 does not currently list producing them.

---

## Carried forward

- **plan.md and the code disagree about determinism.** Subphase 1.1 calls for a fixed temperature and a fixed seed. The Claude 5 family rejects `temperature`, `top_p` and `top_k` with a 400 and exposes no seed. The policy in `helix/llm.py` is therefore a pinned model plus a pinned effort level, with reproducibility resting on the tolerance band over repeated runs that 1.11 already requires. **plan.md has not been edited to match.**

- **`payment-api` location was a judgement call.** CONTEXT.md section 21 has no home for a demo app, so it went to `sandbox/payment-api/`, which marks it as the thing to be broken rather than part of Helix.

- **Commits carry no co-author trailer.** CLAUDE.md section 7 and CONTEXT.md section 23 forbid it, and that overrides the harness default.

- **Still unanswered from plan.md:** whether a hard DJS23OLOE501 submission deadline should compress Phase 2 scope. Does not block anything. If the schedule tightens, cut in the order given under Scope Compression Order.
