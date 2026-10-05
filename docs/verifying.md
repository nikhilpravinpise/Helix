# Verifying Helix yourself

Every command here has been run. They are ordered by what they cost you:
the first tier needs nothing, the last needs a cluster.

If you only run one thing, run tier 1.

---

## Tier 1 - nothing required (~30 seconds)

```bash
make lint      # ruff check + format --check
make test      # 1408 unit tests
```

Expected: `All checks passed`, then `1408 passed, 50 deselected`.

The 50 deselected are the integration tier - they need live
infrastructure and are excluded from every bare `pytest` by design (D9),
so a clean machine never collects them.

**What this does not tell you.** A green suite here is weak evidence, and
this project has the scars to prove it: arm C raised `KeyError` on every
invocation for three subphases with the suite green throughout, because
nothing had ever built its graph. Unit tests cover code that is exercised.
Tiers 2 and 3 are where behaviour is actually checked.

---

## Tier 2 - Postgres only (~2 minutes)

```bash
make db-up && make db-migrate
```

### The benchmark, rules arm

```bash
bash scripts/benchmark.sh
```

Expected: the Postgres-only scenarios run (fixture signals plus the
seven `flow: change` scenarios) and the six live scenarios report
`unmet requirements: kind, elasticsearch`. Skips are honest - they are never counted as passes.

Arm A is a deterministic rule table keyed on fault signature
(`agent/rules.py`), so **it scores 1.00 by construction**. It is the
control, not a result: if it ever reads anything other than 1.00, the
harness is contaminated, not improved.

### The ablation, all five arms

Needs `HELIX_LLM_API_KEY` in `.env`. Costs real tokens.

```bash
bash scripts/benchmark.sh --arm A --arm B --arm C --arm D --arm E
```

Read `benchmark/reports/latest.md`. Check arm A first - if it is not
1.00, discard the run.

### The operator UI

```bash
make serve     # then open http://127.0.0.1:8787/ui
```

### Event layer

```bash
make kafka-up                  # adds ~1GB, see the RAM note below
uv run helix consume --from-beginning
uv run helix emit
bash scripts/event-metrics.sh
bash scripts/correlation-accuracy.sh
```

### Integration tests

```bash
make test-integration
```

Anything needing kind, Elasticsearch, Jenkins or GitHub will fail rather
than skip - a known wart, left deliberately, since a hard failure is
arguably right for a tier you only run on purpose.

---

## Tier 3 - the full sandbox (~10 minutes, RAM-hungry)

```bash
bash scripts/up.sh           # kind + images + ingress + manifests
bash scripts/logging-up.sh   # Elasticsearch + Kibana
bash scripts/jenkins-up.sh   # Jenkins (+ SonarQube)
```

Now all 19 benchmark scenarios run instead of the 13 Postgres-only ones.

**Read `docs/bring-up.md` before this.** It documents the prerequisites
and the hosts-file entry the ingress needs.

---

## RAM

This machine has 15.6GB with roughly 11GB held by unrelated workloads and
swap frequently full. The stacks do not all fit at once.

| Stack | Cost | Needed for |
|---|---|---|
| Postgres | ~200MB | Everything |
| Kafka | ~1GB | Event layer only - the benchmark does not use it |
| ELK | ~1.8GB | The 5 Kubernetes scenarios |
| kind | ~2GB | The 6 live scenarios |
| Jenkins + Sonar | ~1.5GB | CI pipeline checks |

Bring up only what you need, and `make db-down` / `make kafka-down` when
finished. Both keep their volumes, so incident history and the event log
survive.

---

## Reading a result honestly

Three traps this project has already fallen into, all of which produced
numbers that looked entirely plausible:

**Arm A's 1.00 is not a system result.** It is a lookup table returning
its own answer key. It says the harness works.

**`recovered` is Helix's own verdict; `measured` is the harness checking
independently.** They disagree, and the disagreement is the point. In the
one valid four-arm run, arm E reached `recovered` on four of six
scenarios and measured **zero** - it had opened fix PRs against
`sandbox/payment-api/src/main/java/...`, a Java tree in a Python project.
Its own verification passed them. Read `measured`.

**A rate with no denominator is not a measurement.** Duplicate handling
reports 1.00 over *zero redeliveries*, which proves nothing, and says so
beside the rate.

---

## What is not verifiable yet

- **Three-run stability** (subphase 1.11). Needs three consecutive runs
  in band; the daily token cap stopped the last attempt.
- **Live GitHub writes.** No `HELIX_GITHUB_TOKEN`, so `open_fix_pr`
  records an intended write rather than opening a PR. Worth knowing
  before setting one: arm E's hallucinated paths were harmless only
  because of this.
- **Cloud deployment.** `bash scripts/gcp-up.sh` refuses to run and
  prints what is required (D23, `infra/gcp/README.md`).
