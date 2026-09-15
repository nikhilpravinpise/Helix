# Phase 1 Remaining Implementation Plan

**Status:** drafted 2026-09-15, audited and corrected same day. Execution plan only - implementation has not started.

**Scope:** subphases 1.3 through 1.13 of [plan.md](plan.md), plus the final Phase 1 handoff document. Subphases 1.1 and 1.2 are complete and verified per [HANDOFF-1.2.md](HANDOFF-1.2.md).

**Sources:** [plan.md](plan.md), [CONTEXT.md](CONTEXT.md), [HANDOFF-1.2.md](HANDOFF-1.2.md). Where this document and plan.md disagree, plan.md wins and the disagreement is a defect in this document.

---

## 1. Where things stand

| Subphase | Status | Evidence |
|---|---|---|
| 1.1 Foundations | Done | `helix/` (config, llm, logging, observability), uv/ruff/pytest, Makefile, GitHub Actions CI |
| 1.2 Sandbox Environment | Done | `sandbox/payment-api/`, kind cluster scripts, Jenkins pipeline, ELK on host, 10-fault catalogue, `fixtures/faults/` |
| 1.3 - 1.13 | Not started | `agent/`, `state/`, `integrations/`, `ui/` are empty placeholders; `docs/` empty |

Existing assets the remaining work builds on:

- `fixtures/faults/` - 10 recorded fixtures. Cluster fixtures carry `pods_payment_api`, `pods_downstream_stub`, `events`, `logs_payment_api`, `logs_downstream_stub`; CI fixtures carry `console_output`. These are the fast iteration path for 1.3 through 1.6: no live cluster needed for unit-level work.
- `scripts/faults/*.sh` + `scripts/reset.sh` + `scripts/faults/run-all.sh` - one-command fault injection with machine-checkable signature assertions. The 1.11 benchmark harness composes these, it does not reimplement them.
- `scripts/query-logs.sh` - the exact bounded log query shape the ELK capability must expose.
- `helix/llm.py` - currently Anthropic (`claude-opus-5` + pinned effort); **being swapped to Groq's OpenAI-compatible API in P0** (D14), which restores temperature+seed determinism. See CF-1.
- `helix/logging.py`, `helix/observability.py` - correlation-id logging and the metrics/tracing surface every later subphase wires into.

**Known latent defect found during planning:** `sandbox/payment-api/k8s/06-ingress.yaml` exists but nothing serves it - no ingress-nginx controller is installed and the kind cluster was created without `extraPortMappings`. Harmless today (the object applies cleanly, just never routes), but it means 1.9's functional check cannot go through ingress and 1.13 must install the controller plus recreate the cluster with port mappings. Tracked in sections 6.7 and 6.11.

## 2. Carried forward from HANDOFF-1.2

These are unresolved items the handoff explicitly left behind. Each has a home below.

| ID | Item | Resolved in |
|---|---|---|
| CF-1 | plan.md says fixed temperature and seed; Claude 5 rejects both, so `helix/llm.py` shipped pinned model + pinned effort and plan.md was never amended. | **Resolved better than planned:** the provider swap (D14) moves the LLM client to Groq's OpenAI-compatible API, which supports `temperature` and `seed` - plan.md's ORIGINAL determinism policy becomes literally implementable. P0: code change to llm.py + a small plan.md note recording the provider, not a rewrite of the determinism section. |
| CF-2 | The sandbox secret never reaches the evidence surface, so the planted-secret redaction test has nothing real to run against. Handoff offers two options. | 1.3, decision D4 (section 3). |
| CF-3 | `query-logs.sh` truncates at 200 lines and reports it on stderr only. An evidence layer reading stdout gets a silent partial window. | 1.3: the ELK capability queries ES REST directly and returns `truncated`/`total` fields in normalized evidence. The shell script stays as a debugging tool only - the capability does not shell out to it. |
| CF-4 | Host memory is the binding constraint (~5GB free). kind, ELK and Jenkins cannot all run at once with confidence (jenkins-compose.yml says so explicitly). Never lower `payment-api` memory limits - that moves the OOMKilled threshold. | Cross-cutting rule C5 + benchmark ordering in 6.9. |
| CF-5 | Cluster target: settled to local kind. All 1.2 deliverables built against it; the "blocked on human decision" section of the handoff is stale - assumption 1 in plan.md stands confirmed. | Recorded here. The D-number sequence in section 3 starts at D3 because the handoff reserved it for the (now moot) GCP question - this plan's D3 is a different decision, so note the reuse. |
| CF-6 | `payment-api` lives in `sandbox/` rather than the CONTEXT.md §21 layout. Precedent: deviations from §21 are recorded, not silently made. `benchmark/` will be another (see 6.9). | Recorded here and in each relevant subphase. |
| CF-7 | Commit and doc rules: no em dash characters, no agent co-author trailer, no hand-editing generated files. | Cross-cutting rule C1. |
| CF-8 | Still unanswered: whether a hard DJS23OLOE501 deadline compresses Phase 2. Does not block Phase 1. | Revisit at the Phase 1 exit gate; note in the final handoff. |

## 3. Decisions to make and record during implementation

plan.md records decisions as D-numbers (D1, D2 exist). Continue that convention. Each of the following is proposed here with a default; the implementing agent confirms or escalates rather than silently picking. Once settled, record the D-number in plan.md and cite it in the relevant commit.

| ID | Decision | Proposed default | Needed by |
|---|---|---|---|
| D3 | Where PostgreSQL runs | New `infra/postgres-compose.yml` on the host, container `helix-postgres`, named volume, pinned image. Helix state is part of the observer plane (like Jenkins and ELK per D1's intent): it must not live inside the cluster being broken. | 1.4 |
| D4 | How to prove redaction on the real pipeline (CF-2) | Both halves of the handoff's option one: (a) add a `secret_info` evidence type to the Kubernetes capability that returns Secret metadata (name, keys, value presence) and never values - then capture `kubectl get secret payment-api-secret -o yaml` into `fixtures/faults/cluster/` during 1.3 (a live-cluster step) so a real API payload carrying base64 data flows through normalization and redaction, asserting no value survives; (b) a synthetic log line containing `Authorization: Bearer sk-sandbox-placeholder` through the ELK capability to prove the redactor itself. This proves the real path, not just a unit test of a regex. | 1.3 |
| D5 | GitHub evidence and write target | The Helix repo itself (`github.com/nikhilpravinpise/Helix`). `payment-api` already lives here, so commits, diffs, PRs and checks are real. Controlled writes restricted to `helix/INC-*` branches touching only `sandbox/payment-api/` paths. Token via `HELIX_GITHUB_TOKEN`, least-privilege scopes on this repo only. | 1.3, 1.8 |
| D6 | Intake delivery for Kubernetes faults | Push model: the fault scripts already verify the failure signature; the benchmark harness (and a thin `scripts/emit-signal.sh` helper) POSTs the verified signal to intake, or calls `helix intake` directly. Jenkins gets a `post { failure }` block in the Jenkinsfile POSTing to `http://host.docker.internal:<port>` (works on Docker Desktop; Linux needs `extra_hosts: host-gateway` on the compose service). No always-on watcher in Phase 1 - that is event-adapter territory (Phase 1.5, E3). | 1.4, 1.11 |
| D7 | LLM structured output | Extend `helix/llm.py` with `complete_structured(prompt, schema)` returning a validated pydantic model - Groq's OpenAI-compatible surface supports JSON mode / structured outputs (`response_format` with a JSON schema), falling back to prompt-instructed JSON + validation if a model refuses it. Keeps the determinism policy single-sourced; agents never parse raw text. | 1.5 |
| D8 | Ablation arm mechanism | Arms are graph-composition config (`HELIX_ARM=A|B|E`), not code forks: arm E is the full graph, B collapses diagnosis+planning into one LLM call and drops the validate/verify/replan nodes, A replaces LLM nodes with deterministic rule tables. Designed into the graph from 1.5 so 1.11 is wiring, not surgery. | 1.5, 1.11 |
| D9 | Test tiers | `@pytest.mark.integration` marks anything needing live kind/ES/Jenkins/Postgres/LLM. Enforcement via `addopts = "-m 'not integration'"` in pyproject so EVERY pytest invocation excludes them by default - including CI's bare `uv run pytest` (ci.yml needs no change). `make test-integration` runs `uv run pytest -m integration` (a CLI `-m` overrides the addopts default). Unit tests run on fixtures and fake LLM only, so a clean machine is green. | P0, all subphases |
| D10 | GitHub-side benchmark scenarios | Real where cheap, fixture where not: `bad-deployment-config` already has a live fault script. `rejected-pr`/`failed-check`/`stale-branch` are captured as fixture payloads (a crafted PR review, check run, branch state) rather than live PR churn - the scenario injects fixture evidence, and its "recovery" is the recorded action (e.g., `open_fix_pr` issued). Write calls in fixture mode go to a fake transport that records them; live GitHub writes are verified by integration tests, not by the benchmark mutating the real repo every run. Flag in the handoff if this weakens the benchmark claim. | 1.11 |
| D11 | UI stack | Server-rendered: FastAPI + Jinja2 templates + static CSS + minimal vanilla JS polling for live status. No node toolchain, no build step, vendored assets only. Served by the same `helix serve` process as the API. | 1.12 |
| D12 | Terraform scope on a kind target | Terraform manages the kind cluster (kind provider, including the port-mapping config that 1.13 needs), the host-side containers (docker provider: ES, Kibana, Jenkins, Postgres), and the k8s objects (kubernetes provider). `scripts/*.sh` remain the escape hatch; terraform is the documented path. Any piece that resists is documented in the handoff, not silently dropped. | 1.13 |
| D13 | Workflow suspend/resume mechanism | LangGraph checkpointer (`langgraph-checkpoint-postgres`, `thread_id` = incident id). This is what makes "suspends the workflow and persists the pending decision" real: a run suspended at AWAITING_APPROVAL can be resumed by any process - `helix run`, `helix approve`, or the API - because state lives in Postgres, not memory. Also gives crash recovery and the 1.10 run-guard a substrate. If the checkpointer API doesn't fit, fall back to: suspended runs exit cleanly and re-invoke the graph with persisted `WorkflowState`. Record whichever lands. | 1.5, 1.8, 1.10 |
| D14 | LLM provider | **Groq via the OpenAI-compatible API** (`openai` SDK, `base_url=https://api.groq.com/openai/v1`, free tier). Rationale: user requirement (free provider), and it restores real determinism - Groq supports `temperature` and `seed`, which Claude 5 rejected (CF-1). Config keys are provider-neutral (`HELIX_LLM_BASE_URL`, `HELIX_LLM_MODEL`, `HELIX_LLM_API_KEY`), so the same single client also serves Ollama locally or any OpenAI-compatible endpoint - that is the "any free provider" ask without a bespoke multi-provider abstraction (which stays deferred per plan.md). | P0, all LLM work |

## 4. Cross-cutting rules for every subphase

- **C1. House rules.** No em dash characters anywhere (code, docs, commits, UI copy). No agent co-author trailer on commits. Commit style matches `git log` ("Add X", "Correct Y").
- **C2. PR per subphase.** One branch and PR per subphase (or pre-work item), matching how 1.1/1.2 landed. Never commit to `main` directly.
- **C3. Tests are the gate.** Every deliverable lands with tests covering the happy path and the unhappy paths its subphase lists. Never weaken a test to make it pass. Fixtures are the unit-test substrate; live-infra tests carry the `integration` marker (D9).
- **C4. Observability is not optional.** Every workflow step uses `trace_workflow_step`, every LLM call increments `llm_call_counter`/`llm_call_latency`, every evidence collection increments `evidence_collection_counter`, every incident-scoped log line carries the correlation id. This is the only source 1.11's MTTR and Phase 1.5's latency metrics have.
- **C5. Memory discipline.** Never run kind + ELK + Jenkins simultaneously unless a step requires it (jenkins-compose.yml documents the ceiling). The 1.11 harness orders scenarios so the heaviest combination never overlaps. Never lower `payment-api` memory limits (CF-4).
- **C6. Redaction boundary.** Nothing leaves `integrations/` unredacted. Every capability's output passes the redactor before return - asserted by test, not by convention.
- **C7. Docs land with code.** `docs/` is empty. Each subphase adds what its area needs: `docs/architecture/` for design decisions, `docs/runbooks/` for operations, `docs/benchmark/` for 1.11 outputs. No separate documentation pass at the end.
- **C8. Vendor vocabulary stops at the capability boundary.** Agents see `Evidence`, `Action`, `Hypothesis` - never a Jenkins build object or a V1Pod. If an agent file imports a vendor SDK type, that is a defect.
- **C9. Explicitly deferred stays deferred.** No Kafka, event bus, Slack, Jira, Notion, Neo4j, GraphRAG, retrieval layer, MCP, or multi-provider LLM anywhere in Phase 1 code - including imports and dead scaffolding. `events/` and `graph/` stay as `.gitkeep` placeholders through all of Phase 1.
- **C10. Config grows explicitly.** Every new `HELIX_*` key lands in `helix/config.py` AND `.env.example` in the same PR, with a comment. New Makefile targets (`serve`, `db-up`, `benchmark`, `test-integration`) land alongside the feature they expose.

## 5. Work order and dependency map

```text
P0 pre-work (CF-1 doc fix + pytest addopts + wheel packages + CLI stub)
  |
1.3 Evidence layer          - everything else depends on this
  |
1.4 Incident state + intake - needs 1.3's signal shapes; Postgres (D3)
  |
1.5 Coordinator + Diagnosis - needs 1.3 capabilities + 1.4 state; LangGraph, D7, D8, D13
  |
1.6 Recovery Planner        - needs hypothesis from 1.5; scenario action-sets authored here
  |
1.7 Validation Agent        - needs 1.6 candidates; single-retry hook only
  |
1.8 Policy + Execution      - needs 1.6 catalogue + 1.7 validation results; D13 resume
  |
1.9 Verification + Provenance - needs 1.8 execution records; Selenium via port-forward
  |
1.10 Unhappy paths          - hardens the whole loop built in 1.5-1.9
  |
1.11 Benchmark              - needs the full loop + fault catalogue + scenario files
  |
1.12 Operator UI            - needs 1.4 API + 1.8 approvals + 1.9 provenance;
                              can start once 1.9 lands
  |
1.13 Infra formalization    - independent of agent work; can overlap 1.11/1.12
  |
FINAL: Phase 1 handoff doc  - after 1.13 verification passes
```

1.3 is the only true serial blocker. 1.12 and 1.13 may overlap the benchmark work, but each still lands as its own PR with its own exit check.

---

## 6. Subphase work plans

### 6.0 - Pre-work (P0)

Small PR before any feature code - docs plus the repo-config fixes the audit found, plus the provider swap:

- **LLM provider swap (D14, resolves CF-1):** rewrite `helix/llm.py` on the `openai` SDK pointed at Groq (`HELIX_LLM_BASE_URL` default `https://api.groq.com/openai/v1`, `HELIX_LLM_MODEL` pinned - default the current free-tier flagship e.g. `llama-3.3-70b-versatile`, confirmed current at implementation time, `HELIX_LLM_API_KEY`). Determinism policy becomes the real thing plan.md 1.1 asked for: pinned model + `temperature=0` + fixed `seed` (confirm `seed` support on Groq's compat surface when implementing; `temperature=0` is the primary pin regardless). Dependencies: remove `anthropic`, add `openai` via `uv add`. `.env.example` swaps `HELIX_ANTHROPIC_API_KEY` for the three `HELIX_LLM_*` keys. `tests/test_llm.py` is updated - its `MODEL == "claude-opus-5"` assertion pins a provider that no longer exists; this is a legitimate requirements change, not weakening a test.
- Amend plan.md minimally: record D14 (provider = Groq/OpenAI-compatible) and update the 1.1 determinism note to name the provider - the temperature+seed policy itself now stands as written (CF-1 resolved by making the code match the plan, not by editing the plan to match code drift).
- Add a Decisions ledger skeleton (D3-D14) to plan.md, filled per subphase as decisions are ratified.
- **Test-tier plumbing (D9):** register the `integration` marker and set `addopts = "-m 'not integration'"` in `[tool.pytest.ini_options]`, so the bare `uv run pytest` that ci.yml runs can never collect integration tests on a clean runner. Makefile gains `test-integration` (`uv run pytest -m integration`) and `test-all` (`uv run pytest -m "integration or not integration"`).
- **Packaging fix (found in audit):** `[tool.hatch.build.targets.wheel] packages = ["helix"]` only installs `helix`. Once `agent`, `state`, `integrations` hold code, `uv run helix ...` (console script) cannot import them - they only resolve under pytest's `pythonpath=["."]`. Set `packages = ["helix", "agent", "state", "integrations"]` now (all three already have `__init__.py`), and add any further top-level package the moment it gains one.
- **CLI skeleton:** create `helix/cli.py` (stdlib `argparse`, no new dep) with a stub `main()` plus `helix/__main__.py`, and `[project.scripts] helix = "helix.cli:main"` - the entry point and the packaging fix are proven together, then subphases add commands.
- Create `docs/` skeleton: `docs/architecture/`, `docs/runbooks/` with a one-line README.

**Exit:** PR merged, `make lint`/`make test`/`make test-integration` all work, `uv run helix --help` imports cleanly outside pytest.

---

### 6.1 - Subphase 1.3: Evidence Layer (Read-Only Capability Interfaces)

**Goal:** agents can obtain facts without ever touching a vendor SDK.

**New dependencies:** `httpx`, `kubernetes`, `PyYAML`. (Add via `uv add`; pinned floors, versions at least a week old.)

**New config keys (C10):** `HELIX_GITHUB_TOKEN`, `HELIX_GITHUB_REPO`, `HELIX_JENKINS_URL`, `HELIX_JENKINS_USER`, `HELIX_JENKINS_TOKEN`, `HELIX_ES_URL`, `HELIX_KUBE_CONTEXT` (default `kind-helix-sandbox`), `HELIX_EVIDENCE_SOURCE` (`live|fixtures`), `HELIX_FIXTURE_PATH` (which fixture a fixture-mode run replays - the benchmark harness sets it per scenario).

**Files to create**

```text
integrations/
├── evidence.py            # pydantic models: Evidence, EvidenceKind; error hierarchy
├── redaction.py           # redact() applied inside every capability's return path
├── capabilities.py        # Protocol per domain: GitHub/Jenkins/Kubernetes/Docker/Elk evidence
├── fixtures.py            # FixtureStore over fixtures/faults/**; fixture-backed capability impls
├── github/client.py       # commits, diffs, PRs, checks via REST (httpx)
├── jenkins/client.py      # builds, stages, console log, test results via REST
├── kubernetes/client.py   # pods, deployments, events, secret_info via python client
├── docker/client.py       # host-daemon image/container inspect (thin - see scope note)
└── elk/client.py          # bounded log query direct to ES REST (query-logs.sh shape)
tests/integrations/        # per-capability normalization + failure-mode + redaction tests
```

**Design**

- `Evidence` (pydantic, normalized): `id` (stable content hash - hypotheses cite it, 1.9 provenance resolves it), `source` (`github|jenkins|kubernetes|docker|elk`), `kind` (`pod_status|pod_list|event_list|log_lines|build_console|test_results|commit|diff|pr_info|pr_review|check_run|branch_state|secret_info|image_info|container_state`), `service`, `collected_at`, `summary`, `payload`, `truncated: bool`, `total: int|None`, `redacted: bool`, `origin` (`live|fixture` - provenance must show fixture vs live origin).
- Error hierarchy: `EvidenceError` -> `EvidenceAuthError`, `EvidenceTimeoutError`, `EvidenceRateLimitError`, `EvidenceNotFoundError`, `EvidencePartialError`. Each capability maps its vendor errors onto exactly these - uniform handling of auth failure, timeout, rate limit, not found, partial data.
- `redact()`: recursive over dicts/strings. Patterns: `Authorization: Bearer ...`, `sk-*`-shaped tokens, `api_key`/`password`/`token`/`secret` keys at any depth, connection strings, and Kubernetes Secret `.data`/`.stringData` values (replaced with `<redacted>`, key names preserved). Runs inside each capability's return path, not at call sites (C6).
- `secret_info` evidence (D4): returns Secret name/namespace/key names - never values. Capturing the real `kubectl get secret -o yaml` fixture is a live-cluster step inside this subphase.
- ELK capability: same query shape as `scripts/query-logs.sh` (service, window, level, 200-hit cap) but direct to ES REST via httpx, returning `truncated`/`total` explicitly (CF-3). The script stays a debug tool; the capability does not shell out to it.
- **Docker capability scope (corrected):** the host Docker daemon cannot see `payment-api` pods - they run in containerd inside the kind node. This capability covers host-daemon state only: `payment-api:kind` image presence/digest, the kind node container, Jenkins/ES containers. Pod container state belongs to the Kubernetes capability. Sized accordingly - it is the thinnest integration.
- Fixture-backed implementations: each protocol gets a `Fixture*` impl reading `fixtures/faults/*.json` (cluster fixtures map to kubernetes+elk evidence; CI fixtures to jenkins), selected by `HELIX_EVIDENCE_SOURCE`. Unit tests and fixture-mode runs never touch infra.

**Tests**

- Each of the 10 fixtures normalizes through the right capability into `Evidence` objects with expected kind/source/summary.
- Failure modes per capability: auth, timeout, rate-limit, not-found, partial - via stubbed transport, no live infra.
- Planted-secret tests (D4): real `get secret -o yaml` fixture yields metadata-only evidence with zero base64 values; synthetic `Bearer sk-sandbox-placeholder` log line redacted; redaction verified on normalized output, not pre-normalization.
- Truncation: a >200-line synthetic ES response sets `truncated=True` and `total` correctly.
- Boundary check: `agent/` and capability return types reference only `integrations.evidence` types - no vendor types cross (test or import-lint check).

**Exit check:** plan.md 1.3 criterion - each capability returns normalized evidence against live sandbox (integration-marked) and recorded fixtures, every failure mode tested, planted secret provably redacted.

**Not here:** any write op, any LLM call.

---

### 6.2 - Subphase 1.4: Incident State and Intake

**Goal:** a failure signal becomes a durable incident record.

**New dependencies:** `sqlalchemy` (2.x), `alembic`, `psycopg[binary]`, `fastapi`, `uvicorn`.

**New config keys:** `HELIX_DATABASE_URL`, `HELIX_API_BIND` (default `127.0.0.1`), `HELIX_API_PORT` (default `8787` - avoids colliding with payment-api's 8000, Jenkins' 8080, ES's 9200), `HELIX_INTAKE_TOKEN` (optional shared secret for intake POSTs - empty means local-only trust).

**Files to create**

```text
infra/postgres-compose.yml    # helix-postgres on host, named volume, pinned image (D3)
state/
├── db.py                     # engine/session factory
├── models.py                 # incidents, signals, transitions
├── machine.py                # IncidentStatus enum + transition() with allowed-edge enforcement
├── intake.py                 # signal validation, fingerprint, dedup
└── migrations/               # alembic env + versioned migrations
helix/api.py                  # FastAPI app: intake + read endpoints
helix/cli.py                  # extend the P0 stub: serve, intake, run, approve, approvals, provenance
scripts/emit-signal.sh        # POST a failure signal to intake (D6)
scripts/db-up.sh / db-down.sh
tests/state/                  # machine edges, dedup, intake endpoints
```

**Design**

- CLI surface (all subphases extend this): `helix serve`, `helix intake`, `helix run INC-*`, `helix approve/reject`, `helix approvals`, `helix provenance INC-*`, `helix incidents`. Intake is a function in `state/intake.py` used by BOTH the API endpoint and `helix intake` - so the benchmark harness never depends on the API server running.
- Phase 1 status enum: `RECEIVED, DIAGNOSING, REMEDIATION_PENDING, VALIDATING, AWAITING_APPROVAL, EXECUTING, VERIFYING, RECOVERED, ESCALATED, FAILED`. Note for `docs/architecture/state-machine.md`: CONTEXT.md §14's `EXECUTING -> VALIDATING -> RECOVERED` tail refers to post-deploy validation in the Phase 2 lifecycle; Phase 1 validates pre-execution (plan.md 1.7) and verifies post-execution (1.9), hence the distinct `VERIFYING` status. Phase 2 statuses (TRIAGED, OWNER_ASSIGNED, ...) are out of scope.
- `incidents` columns (CONTEXT.md §13 shape): `incident_id` (`INC-*`), `service`, `environment`, `severity`, `status`, `attempt_count`, `linked_pr`, `linked_deployment`, `created_at`, `updated_at`. `signals` carries the raw payload JSONB plus parsed fields; `transitions` carries incident/from/to/cause/at.
- Severity and environment come from the signal payload with documented defaults (`severity=medium`, `environment=sandbox` for the kind cluster); environment taxonomy is `sandbox|staging|production` - deliberately distinct from `HELIX_ENVIRONMENT` (Helix's own runtime mode, not the incident's target). Record this distinction in state-machine.md so they never conflate.
- `transition()` writes a `transitions` row in the same transaction as the status update - every transition persisted with timestamp and cause by construction.
- Tables land incrementally: 1.4 creates `incidents`, `signals`, `transitions`. Later subphases add `evidence_items` (1.5), `hypotheses` (1.5), `candidates` (1.6), `validations` (1.7), `approvals` + `executions` (1.8), `verifications` (1.9), `workflow_runs` (1.5, hardened 1.10). The schema grows with the code that uses it.
- Dedup: `fingerprint = sha256(source | service | environment | fault_signature)` where `fault_signature` derives from payload (k8s: waiting reason + container name; jenkins: job + failing stage). A signal matching an open (non-terminal) incident's fingerprint attaches as a `signals` row; otherwise a new incident is created.
- **Known limitation, documented now:** dedup is same-source only. One underlying failure surfacing as both a Jenkins and a Kubernetes signal produces two incidents - cross-source correlation is E4 (Phase 1.5). Phase 1 benchmark scenarios emit one signal each, so this does not distort scoring, but it goes in the handoff as a limitation.
- Intake endpoints: `POST /intake/kubernetes`, `POST /intake/jenkins`, plus `GET /incidents`, `GET /incidents/{id}`. Payloads are the Phase 1 signal shapes - a `Signal` pydantic model (`source, service, environment, severity, signature, raw`) defined in `state/intake.py`, shared by API and CLI. Canonical EngineeringEvent is Phase 1.5, not now. `helix intake` prints the created-or-attached `incident_id` so the benchmark harness can drive `helix run` against it.
- Jenkinsfile `post { failure }` block POSTs a build-failure signal to `host.docker.internal` (D6) - one small addition, wrapped (`catchError` / `|| true`) so intake downtime can never change a pipeline outcome.
- Intake -> agent: a direct in-process call hook (plan.md 1.4 blesses this over an event bus); the actual wiring lands in 1.5 when the graph exists.
- Unit tests: fingerprint/edge logic is pure (no DB). DB-backed tests are integration-marked against compose Postgres.

**Tests**

- Same fingerprint 3x -> one incident, three signal rows (the literal exit criterion).
- Every legal transition persists with cause; illegal transition raises.
- Terminal-state incident + same fingerprint -> new incident, not attachment.
- Intake endpoint contract tests (TestClient).
- `helix intake` writes the same rows as the API path.

**Exit check:** 3x same fault signal -> 1 incident / 3 signals; all transitions timestamped with cause.

**Not here:** Kafka, event bus.

---

### 6.3 - Subphase 1.5: Coordinator and Diagnosis Agent

**Goal:** evidence in, root-cause hypothesis out, evidence chain retained.

**New dependencies:** `langgraph` + `langgraph-checkpoint-postgres` (D13 - check current stable API at implementation time).

**New config keys:** `HELIX_MAX_EVIDENCE_ROUNDS` (default 3), `HELIX_ARM` (default `E`).

**Files to create**

```text
agent/state.py               # WorkflowState TypedDict: incident, evidence, hypotheses, candidates,
                             # selected_action, validation/execution/verification, attempts, errors
agent/graph.py               # StateGraph: all Phase 1 node slots + edges; D8 arm composition; checkpointer
agent/agents/coordinator.py  # owns transitions via state/machine, picks next node
agent/agents/diagnosis.py    # bounded evidence collection + LLM interpretation -> Hypothesis
agent/prompts/diagnosis.py   # prompt template (single-sourced)
state/models.py              # + evidence_items, hypotheses, workflow_runs migrations
helix/llm.py                 # + complete_structured(prompt, schema) (D7)
docs/architecture/agent-graph.md
```

**Design**

- `WorkflowState` is the single typed object flowing through the graph; nodes read/write slices. Keep it flat and serializable - it is both the checkpointer payload and the provenance substrate 1.9 assembles.
- **Evidence is persisted:** every collected `Evidence` lands in `evidence_items` (incident_id, evidence id, source, kind, summary, payload, origin, collected_at). The 1.5 exit criterion - "the full evidence chain is reconstructable from stored state" - is a DB query, not a memory artifact.
- Coordinator = node + routing function: reads `WorkflowState` + incident status, drives `transition()`, returns next node key. No node mutates incident status directly.
- Diagnosis Agent: (a) deterministic pre-selection by signal source (cluster fault -> pods/events/secret_info/logs; CI fault -> console/test_results/diff), then a bounded LLM loop may request additional evidence up to `HELIX_MAX_EVIDENCE_ROUNDS`; (b) interpretation via `complete_structured` into `Hypothesis(claim, evidence_refs, confidence, contradicting_signals)`. Evidence enters prompts as summaries - full payload retrievable by ref - so prompts stay bounded.
- `evidence_refs` validated against collected evidence; a citation of never-collected evidence is a structured-validation failure (retry once, then escalate - full handling 1.10).
- Checkpointer (D13): `thread_id` = incident_id. Enables: suspend at AWAITING_APPROVAL (1.8), resume by any process (`helix run`, `helix approve`, API), crash recovery, and the `workflow_runs` single-active-run guard (basic here, hardened in 1.10 - no window where two runs can double-execute).
- `helix run INC-*` drives a run to terminal state OR suspension and returns; `helix approve` records the decision and resumes via checkpoint. The benchmark drives the loop entirely through CLI - no API server required.
- D8 seams: arm config selects node composition now; arm implementations land in 1.11.
- Intake wiring: API intake (and `helix intake --run`) triggers the graph in-process per plan.md 1.4's direct-call note.

**Tests**

- Unit (fake LLM): pre-selection picks the expected capability set per signal type; structured-output validation rejects bogus evidence refs; round cap enforced.
- Integration (real LLM): the FIRST such test is a live round-trip of `complete()` and `complete_structured()` against Groq - the pinned model id, `temperature=0`/`seed` acceptance, and JSON-mode structured output have never been verified against the real API, and this is where any drift surfaces. Then crashloop, oom-killed, missing-secret - three distinct signatures - through diagnosis on fixtures; each hypothesis's cited refs exist and support the claim. Run live, results recorded in the PR (this is the exit criterion).
- Evidence chain reconstructable: query `evidence_items` by incident returns the full ordered chain.
- Checkpoint round-trip: run, suspend, resume in a new process, same state.

**Exit check:** plan.md 1.5 criterion on >=3 distinct faults.

**Not here:** proposing or taking any action.

---

### 6.4 - Subphase 1.6: Recovery Planner

**Goal:** candidate actions, ranked, with reasons.

**Files to create**

```text
agent/actions/catalogue.py        # ActionSpec registry - the bounded catalogue
agent/agents/planner.py           # candidate generation + deterministic ranker
agent/prompts/planning.py
state/models.py                   # + candidates table migration
benchmark/scenarios/*.yaml        # action-set fields authored HERE (see design note)
docs/architecture/action-catalogue.md
```

**Design**

- `ActionSpec`: `name`, `params` (pydantic model), `risk_tier` (`low|medium|high` - **a function of params, not always a constant**: `reapply_manifest` on `03-secret.yaml` writes `stringData` and is a secret change rating `high`; on the ConfigMap it rates `medium` - param-dependent tiering is what makes the access table honest), `preconditions`, `expected_effect`, `reversibility` (`stateless|compensable|irreversible`), `compensating_action` (required when `compensable`), `validator` key (1.7), `executor` key (1.8).
- **Reversibility model (corrected):** three classes, not a bool. `stateless` = no persistent change, nothing to undo (restart, rerun_pipeline) -> failed verification goes straight to replan. `compensable` = persistent change with a declared compensating action (scale -> scale back; reapply -> apply previous manifest; open_fix_pr -> close PR; rollback -> redeploy prior revision) -> failed verification runs compensation first, then replans. `irreversible` -> failed verification escalates, never retries. This is what 1.10 consumes.
- Phase 1 catalogue (non-prod sandbox):
  - `restart_deployment(deployment, namespace)` - low, stateless. Targets any deployment in the namespace (dependency-failure restarts `downstream-stub`, not `payment-api` - params are not hardcoded to the demo service).
  - `rollback_deployment(deployment, namespace)` - medium, compensable (roll forward).
  - `scale_deployment(deployment, namespace, replicas)` - low/medium, compensable (scale back).
  - `reapply_manifest(path)` - medium, compensable; path must be a committed file under `sandbox/payment-api/k8s/` (allowlist enforced at param validation).
  - `open_fix_pr(branch, files, title, body)` - medium, compensable (close PR). File contents are produced by the planner via `complete_structured` (path + full new content), constrained to `sandbox/payment-api/` paths; 1.7 validates the content before the PR opens; 1.8's GitHub client does REST writes (create ref -> commit -> PR), never local git, never a protected branch.
  - `rerun_pipeline(job)` - low, stateless.
- Planner: `complete_structured` proposes candidates (action + params + rationale citing evidence refs) constrained to the catalogue; a **deterministic Python ranker** scores over evidence support, risk tier, expected effect, blast radius. LLM proposes, code ranks - ranking is auditable and testable without a model.
- Explicit terminal outcome: `escalate` when no candidate clears the safe bar or confidence is low.
- **Fault -> action -> policy mapping:** `docs/architecture/action-catalogue.md` carries a worked table - each of the 10 catalogue faults, its valid actions, their tiers, and which are approval-gated. This is where it becomes visible that restoring a Secret (`reapply_manifest` on `03-secret.yaml`) is a secret change under the access table and therefore approval-gated - the benchmark's `approval_gated` scenarios fall out of this table rather than being invented.
- `candidates` rows store the FULL ranked list per attempt (every candidate, its rank, rationale, cited evidence refs) - the provenance timeline's "candidates" stage needs all of them, not just the winner.
- **Scenario files start here (corrected):** plan.md 1.6's exit criterion is "for each benchmark scenario" - so `benchmark/scenarios/*.yaml` are authored in 1.6 with the fields planner tests need (`name`, `fault`, `valid_actions`, `unsafe_actions`, `optimal_action`, `approval_gated`) and extended in 1.11 with `root_cause`, `expected_final_state`, fault hook. One source of truth, no duplicated taxonomy. Consistency test: every scenario's `optimal_action` exists in the catalogue, and no `unsafe_actions` entry is missing a catalogue entry it could be confused with.

**Tests**

- Per fault: non-empty ranked list, top candidate in that scenario's valid set, never unsafe-first.
- Param bounds enforced at validation: scale to 0 rejected, manifest path outside allowlist rejected.
- `compensable` action lacking `compensating_action` fails catalogue registration (registry integrity test).
- Empty/unsafe-only proposals -> `escalate`.
- Ranker determinism: same inputs, same order, no LLM.

**Exit check:** plan.md 1.6 criterion - ranked list per scenario, top in valid set, never unsafe-first.

**Not here:** executing anything; validation strategies.

---

### 6.5 - Subphase 1.7: Validation Agent

**Goal:** check the selected action before it touches the system, when checking is practical.

**Files to create**

```text
agent/agents/validation.py   # ValidationResult(PASS|FAIL|NOT_VALIDATABLE) dispatch
agent/validation/            # one strategy module per action family
state/models.py              # + validations table migration
```

**Design**

- Strategies per action:
  - `restart_deployment`/`rollback_deployment`/`scale_deployment`: target exists, `kubectl auth can-i` precheck, param sanity.
  - `reapply_manifest`: `kubectl apply --dry-run=server` on the committed manifest + path inside the allowlist.
  - `open_fix_pr`: config schema check (required keys present, non-empty, right types) + post-change manifest renders + `kubectl apply --dry-run=server` on it.
  - `rerun_pipeline`: job exists and a previous build exists; where no meaningful pre-check exists, honestly `NOT_VALIDATABLE` - never a hollow pass.
- `NOT_VALIDATABLE` carries a reason; coordinator treats it as "proceed to policy/execution decision", never as a pass.
- Outcome persisted (`validations` row) and returned to coordinator; FAIL triggers the single-retry hook back to planning with that candidate excluded (full loop is 1.10).

**Tests**

- Deliberately wrong candidate (config fix missing a required key; manifest failing dry-run) -> FAIL + replan path, not execution.
- `rerun_pipeline` with nothing checkable -> `NOT_VALIDATABLE` with reason.
- Registry completeness: every catalogue action has a validator wired.

**Exit check:** plan.md 1.7 criterion - wrong candidate fails validation and replans rather than executes.

**Not here:** the multi-attempt replanning loop (1.10).

---

### 6.6 - Subphase 1.8: Safety Policy and Execution

**Goal:** actions execute inside enforced boundaries; risky ones stop for a human.

**New config keys:** `HELIX_ALLOWED_KUBE_CONTEXTS` (default `kind-helix-sandbox`), `HELIX_OPERATOR` (default approver identity for CLI decisions).

**Files to create**

```text
agent/policy.py              # decide(action, environment, risk_tier) -> ALLOW | ALLOW_WITH_CHECKS
                             #   | REQUIRE_APPROVAL | DENY
agent/execution/             # executor registry keyed from catalogue
integrations/kubernetes/write.py  # rollout restart/undo, scale, apply committed manifest
integrations/github/write.py      # helix/INC-* branch, commit, PR via REST (D5 scope)
integrations/jenkins/write.py     # trigger build (rerun_pipeline executor; REST + crumb, same auth pattern as capture-ci-fixture.sh)
helix/api.py                 # + GET /approvals, POST /approvals/{id}/approve|reject
helix/cli.py                 # + approve/reject/approvals (records decision + resumes run via D13)
state/models.py              # + approvals, executions migrations
docs/architecture/safety-policy.md
```

**Design**

- Policy table (idea-doc §6 access table, encoded in `docs/architecture/safety-policy.md`):
  - read -> allow (evidence layer, done in 1.3)
  - low-risk write, non-prod -> allow-with-checks
  - medium-risk write, non-prod -> allow-with-checks + verification required
  - any write targeting `production`, secret changes, destructive ops -> require-approval
  - anything outside the catalogue, any direct push to a protected branch -> deny (enforced in code, not just policy output)
- Environment comes from the incident record (set at intake), never inferred. `sandbox|staging` are non-prod; `production` forces the approval path - the exit criterion's production halt is exercised by labeling, which is also how the policy stays honest when real environments arrive later.
- Kubernetes writes: restricted to configured allowed contexts (`HELIX_ALLOWED_KUBE_CONTEXTS`, default `kind-helix-sandbox`). The context check is a hard constraint in the client, independent of policy output.
- GitHub writes: `helix/INC-*` branches, paths under `sandbox/payment-api/` only, PR opened - never merged, never direct-to-main.
- Approval gate: on REQUIRE_APPROVAL the workflow persists the pending action (`approvals` row), transitions to AWAITING_APPROVAL, and suspends via checkpointer. `helix approve INC-*` / `POST /approvals/{id}/approve` records `decided_by` (CLI: `HELIX_OPERATOR` or `--actor`; API: `actor` field) and resumes the run. Rejection routes to replanning (full behavior 1.10). Approval resolution is DB-mediated - process-agnostic, which is what makes CLI, API, benchmark, and UI surfaces equivalent.
- Execution audit: `executions` row per attempt - action+params, policy decision, approver identity, timestamps, result. Written for allowed and approved actions alike.
- **Fake write transports:** each write integration gets a recording fake (`FakeKubernetesWrite`, `FakeGitHubWrite`) selected by evidence-source config - fixture-mode benchmark runs (D10) record intended writes instead of mutating real systems; live mode verified by integration tests.

**Tests**

- Production-tier action -> halts at AWAITING_APPROVAL; executor refuses without a recorded approval.
- Low-risk non-prod action -> executes; `executions` row complete.
- Non-catalogue action -> denied before reaching an executor.
- Protected-branch push attempt -> refused at the GitHub client.
- Approve, reject, malformed-decision paths through CLI and API; suspended run resumes post-approval (checkpoint round-trip).
- Wrong kube context -> refused even when policy would allow.

**Exit check:** plan.md 1.8 criterion verbatim.

**Not here:** auto-merge, any production write beyond the gate, Slack (Phase 2).

---

### 6.7 - Subphase 1.9: Verification Agent and Decision Provenance

**Goal:** prove the system actually recovered; make the whole decision auditable.

**New dependencies:** `selenium` (dev/integration only; selenium-manager handles drivers, host needs a browser installed - documented).

**New config keys:** `HELIX_VERIFY_WINDOW` (default 60s), `HELIX_VERIFY_POLL` (default 20s), `HELIX_ELK_ERROR_THRESHOLD`.

**Files to create**

```text
agent/agents/verification.py # post-action desired-state checks + stability window
state/provenance.py          # assemble + export the full chain per incident
state/models.py              # + verifications migration
tests/e2e/selenium_check.py  # browser leg of functional verification
helix/api.py                 # + GET /incidents/{id}/provenance
docs/architecture/provenance.md
```

**Design**

- Verification checks are per-action desired-state assertions, because "recovered" differs by action: k8s runtime actions -> pods Ready with no bad waiting reasons + rollout status complete; `rerun_pipeline` -> latest build green; `open_fix_pr` -> the PR exists on GitHub (or in the recorded fake-write) and contains the intended changes - the PR IS the remediation for persistent fixes (CONTEXT.md §3: "execute bounded actions OR create a GitHub PR"). Where they apply on top: ELK error rate under threshold + the functional leg.
- Stability window: health must hold across `HELIX_VERIFY_WINDOW` sampled at `HELIX_VERIFY_POLL` - a pod that restarts then crashes again is not recovered. Flapping detection is explicit.
- **Functional leg (corrected):** via `kubectl port-forward svc/payment-api`, not ingress - no ingress controller exists yet (section 1, latent defect; ingress exposure is a 1.13 deliverable per plan.md). This mirrors how the Jenkinsfile already verifies `/ready`. Two legs: Selenium loads the service's `/docs` (FastAPI Swagger UI - a real browser surface requiring zero changes to payment-api), and an httpx-level `POST /payments` round-trip. Browser leg proves HTTP serving end-to-end; API leg proves the business path. `docs/architecture/verification.md` records why this is the honest Selenium use for an API-only service, and that the target moves to `payment-api.local` ingress in 1.13.
- Provenance: assembled from signals/evidence_items/hypotheses/candidates/validations/executions/verifications into one ordered record per CONTEXT.md §15; exportable as JSON (API) and markdown (`helix provenance INC-*`). This is the 1.12 timeline's data source and the handoff's audit artifact.
- Closure: verified -> `RECOVERED`; failed -> compensation/replan or escalation per reversibility class (1.10 completes the loop).

**Tests**

- Recovered scenario -> incident closes, full provenance export.
- Flapping pod fixture -> not recovered.
- Pod Ready but ELK still erroring -> not recovered (the "healthy but serving errors" case).
- Provenance export contains every stage for a completed incident.
- Selenium leg integration-marked (needs live service + browser).

**Exit check:** plan.md 1.9 criterion verbatim.

**Not here:** UI rendering (1.12), ingress (1.13).

---

### 6.8 - Subphase 1.10: Unhappy Paths

**Goal:** the agent's own failure modes are first-class.

**New config keys:** `HELIX_MAX_ATTEMPTS` (default 3), `HELIX_APPROVAL_TIMEOUT`, per-node step timeouts.

**Files to create**

```text
agent/graph.py               # replan/compensate/escalate edges complete
agent/agents/coordinator.py  # retry budget, exclusion list, stall handling
state/models.py              # workflow_runs hardening (idempotency keys, FOR UPDATE)
docs/architecture/unhappy-paths.md
```

**Design**

- Replanning: failed validation or verification -> coordinator re-enters planning with the failed action fingerprint (action+params) excluded from candidates.
- Compensation by reversibility class (1.6 model): `stateless` -> replan directly; `compensable` -> run declared compensating action, then replan; `irreversible` -> `ESCALATED`, never retried.
- Execution failure re-enters `DIAGNOSING` (CONTEXT.md §14's `FAILED -> DIAGNOSING` edge applied to the Phase 1 machine).
- Bounded retries: `HELIX_MAX_ATTEMPTS` per incident, incremented transactionally on the incident row; exhausted -> `ESCALATED` with cause.
- Escalation triggers: low confidence, retry exhaustion, no safe action, irreversible failure, approval timeout. Escalation = terminal status + recorded cause + visible in provenance and UI; no Slack in Phase 1.
- Timeouts: every long-running node (evidence round, validation, execution, verification window, approval wait) has a deadline; stall -> recorded transition, never a hung run.
- Concurrency: `workflow_runs` uniqueness on active-run-per-incident (basic guard from 1.5, hardened here with idempotency keys and `SELECT ... FOR UPDATE` at run start) - duplicate signals and concurrent runs cannot double-execute.
- Test wording note: plan.md 1.10 says "each transition in the CONTEXT.md §14 unhappy path sequence" - the Phase-1-applicable unhappy transitions are: rejected validation -> replan, failed verification -> compensate+replan (or escalate if irreversible), failed execution -> rediagnose, retry exhaustion -> escalate, low confidence -> escalate, no safe action -> escalate, approval timeout -> escalate. The Phase 2 statuses in §14 (TRIAGED, OWNER_ASSIGNED, REVIEW_PENDING) do not exist in Phase 1 and are not tested - record this scoping in unhappy-paths.md.

**Tests**

- Every unhappy transition above: rejected validation -> replan; retry exhaustion -> escalate; low confidence -> escalate; no safe action -> escalate; approval timeout -> escalate; execution failure -> rediagnose.
- Compensable failure invokes its compensating action before replan; irreversible escalates.
- Same signal twice mid-run -> no second workflow run; action not double-executed (concurrent test).
- Repeat-action prevention: replanned candidates never include the failed fingerprint.

**Exit check:** plan.md 1.10 criterion - each unhappy transition tested.

**Not here:** nothing - this subphase exists so these are not an afterthought.

---

### 6.9 - Subphase 1.11: Controlled Benchmark and Evaluation

**Goal:** measured results, not anecdotes. This is the Phase 1 exit gate.

**Files to create**

```text
benchmark/
├── scenarios/*.yaml         # ~12 scenario definitions (action fields from 1.6 extended here)
├── harness.py               # reset -> inject -> signal -> helix run -> score
├── approver.py              # scripted approver (D2)
├── scoring.py               # metric computation
└── reports/                 # generated output
agent/arms.py                # D8 arm composition (A rules-only, B single-agent, E full)
docs/benchmark/              # methodology, tolerance band, latest report
scripts/benchmark.sh         # single-command entry; make benchmark
```

(New root dir `benchmark/` - a recorded §21 layout deviation like `sandbox/`, CF-6.)

**Design**

- 12 scenarios matching CONTEXT.md §18's Phase-1 categories exactly, with an explicit injection mode each:

  | Category | Scenario | Injection |
  |---|---|---|
  | CI/CD | dependency-failure | `fixtures/faults/ci/` replay |
  | CI/CD | test-regression | `fixtures/faults/ci/` replay |
  | CI/CD | docker-build-failure | `fixtures/faults/ci/` replay |
  | CI/CD | config-mismatch | live `scripts/faults/config-mismatch.sh` |
  | Kubernetes | crashloop | live fault script |
  | Kubernetes | oom-killed | live fault script |
  | Kubernetes | failed-rollout | live fault script |
  | Kubernetes | missing-secret | live fault script |
  | GitHub | bad-deployment-config | live fault script |
  | GitHub | rejected-pr | fixture payload |
  | GitHub | failed-check | fixture payload |
  | GitHub | stale-branch | fixture payload |

  Six live, six fixture-replay. The CI faults have NO fault scripts - `capture-ci-fixture.sh` was a one-off uncommitted-edit + Jenkins run - so fixture replay is the honest default: those fixtures ARE captures of real Jenkins failures. A `--live-ci` flag (harness makes the edit, triggers Jenkins, reverts) documents the live path for anyone who wants it; it is not the default because it needs the memory-heavy kind+Jenkins combination (CF-4).
- Approval-gated scenarios are not synthetic: restoring a Secret (`missing-secret`, `crashloop` via `reapply_manifest` on `03-secret.yaml`) is a secret change under the access table, so those scenarios are `approval_gated: true` and exercise the scripted approver for real.
- Each scenario YAML (extending the 1.6 fields): `name`, `category`, `root_cause`, `inject` (`live|fixture`), `fault_script` or `evidence_fixture`, `signal` (the intake payload to emit - the signature the fault script already verified), `requires` (stacks needed: `kind`, `elasticsearch`, `jenkins`, or none), `valid_actions`, `unsafe_actions`, `optimal_action`, `approval_gated`, `expected_final_state`.
- Harness drives CLI only: `reset.sh` (live scenarios; fixture scenarios skip reset) -> inject -> `helix intake` -> `helix run INC-*` to terminal/suspended -> scripted approver resolves pending approvals -> `helix run` resumes -> collect final state + provenance -> score. No API server dependency.
- Scripted approver (D2): approves only where `approval_gated` and the pending action matches `optimal_action`; approval recorded as its own outcome class; any execution without required approval -> unsafe regardless of result. Human intervention rate reported separately.
- Metrics: RCA accuracy (hypothesis vs declared root cause), action validity (selected in valid set, not unsafe), recovery success (final state matches - measured by the harness inspecting actual state, not by the verification agent's verdict, so arm B can be scored), MTTR (signal -> RECOVERED from C4-instrumented timestamps), unsafe action rate.
- Ablation arms via D8: A = honest rules module (`agent/rules.py`: fault-signature -> hypothesis -> action tables, zero LLM calls); B = single LLM call on pre-collected evidence produces hypothesis+action directly - no validate/verify/replan nodes; E = full graph. Same harness, config-selected.
- **Fixture-mode E2E substrate:** `HELIX_EVIDENCE_SOURCE=fixtures` + fake write transports + Postgres gives a full end-to-end workflow run with zero cluster/LLM infra - the cheapest honest way to exercise the whole loop. It becomes the substrate for 1.10's unhappy-path E2E tests and for CI-friendly integration testing.
- **Memory ordering (CF-4):** kind + ELK + Jenkins cannot all co-run - jenkins-compose.yml says so itself. With CI scenarios fixture-mode by default the live needs collapse to: K8s/config scenarios need kind + ES (evidence + error-rate verification), fixture scenarios need only Postgres. Each scenario declares `requires:` and the harness groups runs so stacks are only up when needed. A scenario genuinely needing all three stacks is a defect to escalate, not to silently skip.
- Arm A needs no LLM key; B/E need `HELIX_LLM_API_KEY` (Groq free tier). **Rate-limit reality:** the free tier is ~30 RPM / ~1K RPD for 70B-class models - a full 12-scenario x 3-run grid on arms B+E can approach daily caps. Mitigations, in order: client-side retry with backoff (built into llm.py), scenario/arm subsetting during iteration, and `HELIX_LLM_BASE_URL` pointing at a local Ollama as a zero-limit fallback for dev runs - the exit-gate runs still use Groq so the numbers are comparable. Harness supports `--scenarios`/`--arms` subsetting; the exit gate is the full grid.
- Tolerance band: documented in `docs/benchmark/tolerance.md` before the runs; three consecutive full-grid runs inside the band on every metric is the exit check. Variance beyond the band is a defect against 1.1's determinism policy - investigate, don't widen the band to pass.

**Tests / checks**

- Harness dry-run against a single scenario end-to-end before the full suite.
- Scoring unit tests: each metric on synthetic outcomes, including executed-without-approval = unsafe.
- Arm smoke tests: A/B/E each complete one scenario (integration-marked).

**Exit check:** plan.md 1.11 criterion - one command, scored report, 3 runs in band. This is also the Phase 1 exit gate.

**Not here:** arms C (needs E6) and D (needs 2.7); the 7 org/cross-system Phase 2 scenarios.

---

### 6.10 - Subphase 1.12: Operator Interface

**Goal:** a human can follow an incident without reading the database.

**New dependencies:** `jinja2` (server-rendered per D11 - no node toolchain).

**Files to create**

```text
ui/
├── templates/   # base, incident list, incident detail (provenance timeline), approvals queue
└── static/      # CSS + minimal JS (vendored, no CDN dependency)
helix/ui.py      # routes mounted on the same FastAPI app (`helix serve`)
```

**Design**

- Pages: incident list (status, service, severity, age); incident detail (full provenance timeline: trigger -> evidence -> hypothesis -> candidates -> decision -> validation -> execution -> verification, every entry resolving to stored records); approvals queue (pending actions posting approve/reject to the 1.8 API - which records the decision and resumes the suspended run via checkpoint); live status on in-flight incidents via polling (status reads DB, so it works regardless of which process drives the workflow).
- No auth multi-user machinery - local operator surface, single user (recorded limitation).
- `docs/runbooks/operator.md` lands here: serve/intake/run/approve/provenance CLI + UI walkthrough - the doc the exit criterion gets verified against.
- Pixel standard per the global rules: run a real incident including an approval and inspect every exercised view; fix visual defects found along the way.

**Tests**

- Route tests via TestClient: list renders, detail renders all provenance stages for a fixture-built incident, approval POST drives the decision + resume, empty states render.
- Live-status poll returns current state during an in-flight run (integration-marked).

**Exit check:** plan.md 1.12 criterion - full incident incl. approval followed through UI alone, no visual defects on exercised paths.

**Not here:** dashboards, analytics, roles.

---

### 6.11 - Subphase 1.13: Infrastructure Formalization and Quality Gates

**Goal:** environment reproducible from code; repo holds its own quality bar.

**Files to create**

```text
infra/terraform/             # kind cluster + host containers + k8s objects (D12)
infra/kind-config.yaml       # extraPortMappings for ingress - REQUIRED, see below
infra/ansible/               # localhost playbook: toolchain checks/installs, ES sysctl, dirs
infra/sonarqube-compose.yml  # + SonarQube analysis + Quality Gate stages in Jenkinsfile
infra/jenkins/plugins.txt    # + sonar plugin; infra/jenkins/jenkins.yaml JCasC updated
sandbox/payment-api/k8s/     # + ingress-nginx controller manifests/install step
docs/bring-up.md             # one-command bring-up from a clean machine
README.md                    # updated quickstart reflecting the real Phase 1 surface
```

**Design**

- **Ingress (found during audit):** today `06-ingress.yaml` applies to nothing - no controller, no port mappings. 1.13 installs ingress-nginx into kind and adds `infra/kind-config.yaml` with `extraPortMappings` (80/443), which requires a one-time cluster recreate - `scripts/up.sh` and the Terraform kind config both switch to `--config infra/kind-config.yaml`. Expose `payment-api.local` (works once controller exists). For `helix.local`: Helix runs on the host (it must outlive the broken cluster - it must NOT run inside it), so the honest path is a selectorless Service + manual Endpoints pointing at the host's docker-gateway IP, fronted by the same ingress. Validate this works on kind; if it fights, document host-port access as the path and record the tradeoff in the handoff rather than silently weakening the deliverable.
- Terraform (D12): kind cluster via kind provider (using kind-config.yaml), host containers via docker provider (ES, Kibana, Jenkins, Postgres, SonarQube), k8s objects via kubernetes provider. `scripts/*.sh` stay as the debugging escape hatch; Terraform is the documented path.
- Ansible: host prep only - toolchain checks (docker, kind, kubectl, uv), `vm.max_map_count` for ES, required dirs. Not service config compose already owns.
- SonarQube: compose service + JCasC/plugin wiring + `SonarQube analysis` and `Quality Gate` stages in the Jenkinsfile for the payment-api pipeline.
- Verification's functional check target flips to `payment-api.local` ingress here (1.9 built it against port-forward; update the target via config, keep both paths working).
- Honest exit: a true clean-machine rebuild can't be fully proven on this host - approximate with full teardown (all compose downs + `kind delete` / `terraform destroy`) then rebuild via terraform + documented steps, and record the approximation in the handoff.

**Tests / checks**

- `terraform plan` clean on fresh state; apply produces a working sandbox (integration).
- Jenkins pipeline with quality gate: green on good code, fails on a planted defect.
- Bring-up doc verified by following it literally.
- Selenium functional check passes through `payment-api.local` ingress.

**Exit check:** plan.md 1.13 criterion - rebuild from committed code + documented steps only.

**Not here:** multi-environment or production-grade infra.

---

### 6.12 - FINAL: Phase 1 Handoff Document

**After 1.13's exit check passes**, write `HANDOFF-PHASE1.md` at repo root for the teammate picking up Phase 1.5. Mirror HANDOFF-1.2.md's proven structure:

```text
# Handoff - Phase 1 Complete
**Date:** ...  **Branch:** main  **Commits:** <per-subphase ledger>

## Where things stand
- Status table 1.1-1.13 with per-subphase notes
- Phase 1 exit gate result: benchmark numbers + tolerance-band evidence

## Per-subphase deliverable ledger
- Table per subphase: deliverable | status | note

## What was built
- Annotated tree of agent/, state/, integrations/, benchmark/, ui/, infra/

## Verification
- Re-run table: lint, test, test-integration, benchmark run, clean rebuild,
  bare `uv run pytest` on a clean machine (zero integration tests collected),
  and `uv run helix` importable outside pytest - results copied from actual runs

## Decisions made
- D3-D14 as ratified, with pointers to where each lives in code/docs

## Carried forward / known limitations
- CF-8 deadline question still open
- Same-source-only dedup (1.4); fixture-mode CI and GitHub scenarios (D10);
  clean-machine rebuild approximation (1.13); determinism resting on
  tolerance band (CF-1); whatever surfaced honestly during the work

## Next actions for Phase 1.5
- E1 entry conditions: Phase 1 benchmark green; canonical event taxonomy
  discrepancy to reconcile first (rollout.failed / service.unhealthy appear in
  the idea doc §7 but not CONTEXT.md §7 - plan.md E1 already flags it)
- E1-E7 pointers and what each inherits from Phase 1
```

**Exit check for this step:** the doc exists, every claim in it was re-verified (not copied from reports), and a teammate could start E1 from it alone.

---

## 7. Watch items (risks specific to remaining work)

| Risk | Bites in | Mitigation built into this plan |
|---|---|---|
| LLM non-determinism breaks the tolerance band | 1.11 | CF-1 doc fix + band documented before runs + variance treated as defect |
| LangGraph / checkpointer API drift | 1.5 | Pin at add time; D13 records a fallback (persisted-state re-invoke) |
| Groq model id + `temperature`/`seed`/JSON-mode params never verified against the live API | 1.5 | First integration test is a real round-trip; failure surfaces there, not mid-benchmark |
| Groq free-tier rate limits (30 RPM / ~1K RPD) throttle or block the benchmark grid | 1.11 | Retry+backoff in llm.py; `--scenarios`/`--arms` subsetting; Ollama fallback via `HELIX_LLM_BASE_URL` for dev runs; exit-gate numbers stay on Groq |
| LLM quality drop vs a frontier model (open-weight, 131k ctx) weakens diagnosis/planning | 1.5-1.11 | Honest answer: measure it - the benchmark exists to show it. Prompts sized for 131k ctx; if arm E underperforms arm A that is a finding, not a defect to hide |
| Postgres needed for unit tests on a clean machine | 1.4+CI | Dedup/machine logic pure; DB tests integration-marked (D9) |
| Scope creep in action catalogue | 1.6 | Catalogue sized to scenario valid-action sets; nothing speculative |
| Benchmark flakes from timing-dependent faults | 1.11 | Fault scripts already assert signatures; harness retries count as data, not silent reruns |
| Approval gate deadlocks a benchmark run | 1.11 | Scripted approver (D2) + approval timeout path from 1.10 |
| kind+ELK+Jenkins memory collision during benchmark | 1.11 | Per-scenario `requires:` stack declaration + grouped ordering (C5/CF-4) |
| GitHub writes spam the real repo during benchmark | 1.8/1.11 | Fake write transports in fixture mode (D10); live writes only in integration tests |
| UI regresses unnoticed | 1.12 | Real incident walkthrough is the exit check, not screenshots |
| Ingress dead since 1.2 (no controller) | 1.9/1.13 | Port-forward now; controller + kind-config port mappings in 1.13 |
| Terraform on kind becomes a rabbit hole | 1.13 | D12 fallback rule: document what resisted, keep scripts working |

## 8. Definition of done, applied to every subphase

1. Exit criterion demonstrated end to end, with the command and output recorded in the PR.
2. Tests cover happy path plus the subphase's listed unhappy paths; zero skipped/weakened tests.
3. `make lint` and `make test` green on a clean machine (bare pytest collects zero integration tests); integration tests green where infra allows; `uv run helix` importable outside pytest.
4. C1-C10 hold (em dash scan, no vendor types in `agent/`, observability wired, redaction boundary intact, config keys in `.env.example`).
5. Its doc landed under `docs/` and its D-decisions are recorded in plan.md's ledger.
6. PR reviewed and merged; commits carry no co-author trailer.
