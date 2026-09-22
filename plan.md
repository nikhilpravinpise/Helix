# Helix - Implementation Plan

**Status:** finalized 2026-08-21. Ready to execute from 1.1.

Execution plan derived from [CONTEXT.md](CONTEXT.md) and [Helix_Modular_Project_Idea.md](Helix_Modular_Project_Idea.md).

Phases follow the source documents exactly. This file adds 32 subphases, each with a goal, deliverables, a verifiable exit criterion, and an explicit scope guard.

---

## Current Status

**Last updated: 2026-09-21.** Keep this block current - it is the one
place that answers "how much is done" without reading the whole file.

**31 of 32 subphases complete (97%). Phase 2 is implemented.**

| Phase | Subphases | Status |
|---|---|---|
| Phase 1 - MVP | 1.1 - 1.13 | **13/13 done.** Exit gate met: the benchmark runs unattended and reports all five metrics |
| Phase 1.5 - Event Layer | E1 - E7 | **7/7 done.** All exit criteria met, including E7's ablation. Its open defects are fixed in Phase 2 - recorded below |
| Phase 2 - Full Helix | 2.1 - 2.11 | **11/11 implemented.** Change-review lifecycle, Slack/Notion/Jira/Linear/Trello integrations, Neo4j graph + GraphRAG, arm D, and the expanded benchmark all land and test green - see HANDOFF-PHASE2.md |
| Optional | OPT-1 | Not started |

**Phase 1.5's exit gate is met.** Phase 1 benchmark results reproduce
through the event path with no regression, correlation and replay metrics
are recorded, and ablation arm C is measured against A, B and E
(`docs/benchmark/phase-1.5-evaluation.md`). Phase 2's entry gate is
therefore open.

**What the ablation concluded.** Retrieval earned its place - arm C beats
arm B on every reasoning metric across six runs on two models. The full
graph did not: arm E is the worst LLM arm and the least stable. Arm A
scores 1.00 with zero spread across three runs, which measures the
determinism policy rather than the architecture.

**Phase 1.5's open defects, fixed in Phase 2.** Arm B selected
`rollback_deployment` - a Kubernetes write - in response to a rejected
pull request and a diverged branch, and the policy gate allowed it
unapproved, in 2 of 3 runs. `agent/policy.py` judged an action's risk
tier in isolation and never asked whether the action was coherent with
the evidence that produced it. Phase 2 added the coherence check:
`decide()` now takes the incident's canonical evidence types and
escalates any runtime-mutating action (kubernetes/jenkins executor)
proposed on source-control-only evidence to REQUIRE_APPROVAL. Two
companion gaps closed at the same time: `executing -> failed` now
replans in run for refused executions (bounded by the exclusion
budget), and `validate_fix_content` rejects file paths whose parent
directory does not exist - the hallucinated-directory-tree signature
arm E's self-certification missed.

**Open commitments not covered by any subphase:**

- **GCP / cloud deployment (D23).** DJS23OLOE501 requires it; Helix runs
  on kind. Stubbed, not built: `infra/gcp/README.md` scopes the full
  kind-to-GKE delta and `scripts/gcp-up.sh` is the entry point, which
  refuses to run and prints the prerequisites. Nothing in the repo
  depends on GCP, so this is additive work rather than a migration.
- **Three Phase 1 gaps** carried from HANDOFF-PHASE1.md: no
  `HELIX_GITHUB_TOKEN` (so `open_fix_pr` cannot write for real), Ansible
  validated by syntax only, and Jenkins and SonarQube torn down. (The
  fourth - `executing -> failed` not replanning within a run - is fixed:
  refused executions replan in run, bounded by the exclusion budget.)
- **Arm E's hallucinated-paths finding is mitigated, not closed.** The
  validator now rejects file paths whose parent directory does not
  exist, which catches the src/main/java class of failure. The deeper
  finding - a model can still write plausible-but-wrong content into
  real paths - is why `recovered` and `measured` stay separate columns.

**How to verify any of this yourself:** `docs/verifying.md`.

---

## How to Read This Plan

Subphase numbering avoids collision between "Phase 1 subphase 1.5" and "Phase 1.5":

| Phase | Subphase labels |
|---|---|
| Phase 1 - MVP | `1.1` ... `1.13` |
| Phase 1.5 - Event Layer | `E1` ... `E7` |
| Phase 2 - Full Helix | `2.1` ... `2.11` |
| Optional extension | `OPT-1` |

Every subphase has:

- **Goal** - the single thing it proves.
- **Deliverables** - what lands in the repo.
- **Exit criterion** - an observable check, not an opinion.
- **Not in this subphase** - the guard against scope creep.

**Definition of done for any subphase:** exit criterion demonstrated end to end, tests covering happy path and at least the listed unhappy paths, lint clean, no skipped or weakened tests.

### Assumptions

These are not stated in CONTEXT.md. They are recorded here as assumptions, not decisions, and should be confirmed before subphase 1.1 starts.

1. The runtime target is a local Kubernetes cluster (kind or minikube) rather than a cloud cluster. Rationale: reproducible benchmark scenarios matter more than realistic scale, and CONTEXT.md §18 demands reproducibility.
2. The demo application under test is a small purpose-built service (the `payment-api` used in CONTEXT.md examples), owned by this repo, so faults can be injected deterministically.
3. Single-repo layout as given in CONTEXT.md §21.
4. One LLM provider behind a thin interface. Provider abstraction beyond that is deferred until a second provider is actually needed.

### Decisions

Two of the three questions this plan originally left open are engineering calls, and they are now settled. Both are recorded here and wired into the subphase that depends on them.

**D1. Jenkins runs in Docker on the host, not in the cluster.** Wired into 1.2. The cluster is the thing being deliberately broken, so CI must not share its failure domain. An in-cluster Jenkins would go down with the fault it is supposed to observe and remediate, and would contaminate benchmark runs with failures that belong to the harness rather than the scenario.

**D2. Benchmark runs use a scripted approver, and bypassing a gate always scores as unsafe.** Wired into 1.11. Approval-gated scenarios are neither auto-failed nor quietly auto-approved. The harness approves only where the scenario declares the optimal action to be approval-gated, records that approval as its own outcome class, and counts any action executed without a required approval as unsafe regardless of whether it worked. Human intervention rate is reported separately so gate frequency stays visible.

**D3. PostgreSQL runs in Docker on the host** (`infra/postgres-compose.yml`, container `helix-postgres`). Helix state is part of the observer plane and must not live inside the cluster being broken - same reasoning as D1.

**D4. Redaction is proven on the real evidence path, not only synthetically.** The Kubernetes capability exposes `secret_info` (Secret metadata, never values); a captured `kubectl get secret -o yaml` fixture carries real base64 data through normalization + redaction, plus a synthetic `Bearer` log line proves the redactor itself.

**D5. The GitHub integration targets this repository.** `payment-api` lives here, so commits/diffs/PRs/checks are real. Controlled writes are restricted to `helix/INC-*` branches touching only `sandbox/payment-api/` paths.

**D6. Intake is push-model.** The benchmark harness / `scripts/emit-signal.sh` / a guarded Jenkinsfile `post { failure }` block deliver failure signals; no always-on watcher exists in Phase 1 (that is E3 territory).

**D7. `complete_structured(prompt, schema)`** on the LLM interface returns a validated pydantic model, via the provider's JSON-schema response format with a prompt-instructed JSON fallback.

**D8. Ablation arms are graph-composition config** (`HELIX_ARM=A|B|E`), not code forks: E is the full graph, B collapses diagnose+plan into one LLM call and drops validate/verify/replan, A replaces LLM nodes with deterministic rule tables.

**D9. Test tiers are enforced by pytest addopts.** `addopts = "-m 'not integration'"` means every pytest invocation - including CI's bare `uv run pytest` - excludes live-infra tests by default; `make test-integration` opts in.

**D10. Benchmark scenarios are live where cheap, fixture-replay where not.** Cluster faults run their real fault scripts; CI faults (which have no fault scripts - they were one-off uncommitted edits) and the rejected-pr/failed-check/stale-branch GitHub scenarios replay captured fixtures, with writes recorded by fake transports rather than mutating the real repo.

**D11. The operator UI is server-rendered** (FastAPI + Jinja2 + static assets, no node toolchain), served by the same `helix serve` process as the API.

**D12. Terraform manages the kind cluster, host-side containers, and k8s objects**; `scripts/*.sh` remain the debugging escape hatch.

**D13. Workflow suspend/resume uses the LangGraph Postgres checkpointer** (`thread_id` = incident id), making approval suspension, crash recovery, and the single-active-run guard real instead of conventional.

**D14a. The pinned model is `openai/gpt-oss-120b`.** Groq decommissioned the Llama 3.3 family, so D14's `llama-3.3-70b-versatile` pin returned `model_not_found` the first time a real key was used - the arms ran and every incident failed with "diagnosis produced no hypothesis", because an LLM error surfaced as an empty result rather than as a named provider failure. `openai/gpt-oss-120b` is the largest reasoning model the free tier now exposes and is verified to satisfy D7's structured-output contract. A pinned model is still the right call for determinism, but a pin is a dependency on someone else's deprecation schedule, and this one expired silently.

**D14. The LLM provider is Groq via the OpenAI-compatible API** (`openai` SDK, `HELIX_LLM_BASE_URL|MODEL|API_KEY`). Groq's free tier supports `temperature` and `seed`, so the determinism policy in 1.1 is implemented literally - replacing the Anthropic client whose model family rejected those parameters. Provider-neutral config means the same single client also serves a local Ollama or any OpenAI-compatible endpoint; a bespoke multi-provider abstraction stays deferred.

**D15. The event taxonomy is CONTEXT.md §7 plus `service.unhealthy` and `branch.diverged`; `rollout.failed` is rejected.** This settles the E1 discrepancy between CONTEXT.md §7 and the idea document. `events/schemas.py` is the single source, and its docstring carries the full reasoning. The test applied to each candidate was whether a signal Helix actually handles has no honest label without it. `rollout.failed` fails that test: `failed-rollout` reports `ImagePullBackOff` on the pod (`pod.failed`) and `config-mismatch` reports `rollout_wedged` with the old ReplicaSet still serving (`deployment.failed`). `service.unhealthy` passes it on the captured `config-mismatch` fault, whose new-ReplicaSet pod is `Running` with its container running and `Ready: False` - up and not serving, so both other labels would be false; it has no classifier rule and is emitted by the E3 Kubernetes adapter instead. (E1 first justified this with `dependency-failure`; E3 checked that fixture and found its pod is `Ready: True`, because the sandbox deployment declares no readiness probe, so a 503 from `/ready` never reaches the pod's conditions. The decision is unchanged, its evidence is corrected.) That the same fault reads as `deployment.failed` from its signal and `service.unhealthy` from its pod is not a contradiction - they describe different objects, and joining them into one incident is what E4 correlation is for. `branch.diverged` was not part of the original discrepancy and was forced by evidence: `stale-branch` signals `{type: branch_state, comparison: diverged}`, the taxonomy's git family is push and pull-request events only, and Helix recovers that scenario today. Compatibility rule: `schema_version` is `MAJOR.MINOR`, additive change is MINOR, and consumers must ignore unknown `metadata` keys and skip event types they do not handle.

**D16. Intake stays total: an unclassifiable signal opens an incident with no canonical event.** Rejecting it at the front door would drop a real failure, which is the one thing an incident recovery system must not do, and it matches how the rest of Helix treats an unrecognised signature - the rules arm escalates rather than refusing. The miss is recorded in the creation transition so it appears in provenance. `events.classify.classify` itself still raises, because the E3 adapters should fail loudly rather than emit an unnamed event.

**D17. The event backbone is a single-broker Kafka in KRaft mode, keyed on `correlation_id`.** `infra/kafka-compose.yml`, brought up with `scripts/kafka-up.sh`; `events/broker.py` holds the wrappers and the full reasoning. Topics are declared by the script rather than auto-created, because partition count and retention are design: `helix.events` has 3 partitions and 7-day retention, `helix.events.dlq` has 1 partition and 30-day retention, deliberately longer because a dead letter is evidence and outlives the event that caused it. The partition key is `correlation_id` falling back to `service`, which is what makes per-incident ordering true while still allowing a consumer group to scale. Delivery is at-least-once in both directions: the producer uses `acks=all` with idempotence, and the consumer disables auto-commit and commits only after the handler returns, so handlers must be idempotent. A record that does not deserialize goes to the DLQ verbatim with the reason in its headers and is then committed, since it can never succeed and would otherwise block every event behind it; a handler that raises does **not** commit, because that event is well-formed and discarding it would lose a real failure. Single broker means replication factor 1, which is correct for a sandbox and wrong for production; the acknowledgement and retention settings are the parts that carry over.

**D18. Adapters own vendor knowledge, derive idempotent event ids, and redact before emitting.** `events/adapters/` holds one module per source (Kubernetes, Jenkins, GitHub, ELK). They are pure functions over already-fetched payloads - fetching stays `integrations/`' job, which is what lets every adapter be tested against a recorded fixture with no network. Adapters do not call `events.classify`: that exists to guess a type from a Phase 1 signal signature, while an adapter holds the vendor payload and knows the answer outright. Event ids come from `derive_event_id` over the payload's natural key (Kubernetes object uid plus restart count, Jenkins job plus build number, GitHub review id, Elasticsearch document id), so a redelivered webhook yields a byte-identical id; the key may contain only fields the vendor repeats verbatim. Every adapter builds events through `vendor_event`, which redacts `metadata` - the event stream is a second path into agent context, and subphase 1.3 made redaction a guarantee that has to hold on both. Console logs are clipped from the middle rather than the end, keeping both the failure detail and the build verdict.

**D19. Correlation is token-based and time-bounded, and ingest never executes anything.** `events/correlation.py` extracts tokens from each event along the dimensions plan.md E4 names - service and environment, commit, PR, branch, build, pod, object - and `events/ingest.py` attaches an event to the open incident sharing any token. Linking is transitive in effect, which is what lets a GitHub check, a Jenkins build, a Kubernetes pod and an ELK log line converge without any pair sharing every token: check and build share a commit, build and pod share a service, pod and log share a pod name. Two bounds keep the service token from merging everything on a single-service sandbox: only non-terminal incidents are candidates, and correlation requires the incident's most recent event to be inside `CORRELATION_WINDOW` (30 minutes). The window is there because a stale open incident left by a killed run is a documented occurrence, and without it one would silently absorb every later event for its service while looking exactly like correlation working. Replay is a database fact rather than a convention: `event_records.event_id` is UNIQUE and ingest returns the original incident without writing. Ingest opens incidents and attaches events and never advances one past RECEIVED, so replaying any number of events cannot re-execute an action; driving the workflow stays an explicit `helix run`. Merging two already-open incidents is deliberately out of scope - it means reconciling two workflow states, two approval histories and two execution sets, which is a different problem from correlation.

**D20. Helix emits lifecycle events through a transactional outbox, and replay reconstructs the lifecycle rather than every step.** The five CONTEXT.md §7 lifecycle types are derived from status transitions in `events/lifecycle.py` and written to `event_records` inside the same transaction as the `transitions` row they describe, so an event and the state change it reports can never disagree. Emission does not touch Kafka: every state change in Helix passes through `state.machine.transition`, including in unit tests, and publishing inline would make a running broker a prerequisite for all of it while a briefly-unavailable broker would either lose the event or fail the state change. `events/emit.py` drains unpublished rows afterwards (`helix emit`), marking `published_at` only after the producer flush returns, and re-draining is harmless because every id is stable. `remediation.completed` is keyed on reaching `verifying` rather than on leaving `executing`, since `executing -> failed` means no remediation completed. One store holds both consumed and emitted events, because E6 retrieval wants an incident's whole story - but Helix's own events are excluded from `correlated_sources` and from the E4 correlation window, since they record when Helix reacted rather than when anything happened outside, and counting them would let an incident hold its window open by transitioning. **Replay reconstructs the lifecycle, not every transition:** `diagnosing`, `validating`, `escalated` and `failed` have no event in the taxonomy, so `IncidentProjection` is the unit of comparison - milestones reached plus incident identity - and it is built from the incidents/transitions tables on one side and from stored envelopes on the other.

**D21. Retrieval is lexical and in process, and arm C is arm B plus retrieval.** `events/retrieval.py` scores IDF-weighted token overlap between a new incident's fault signature and the flattened provenance of resolved ones. No vector store and no pgvector: plan.md asks for similarity search, not for embeddings, and adding either would mean new infrastructure for a corpus of tens of incidents. The lexical choice also buys two things that matter - it behaves identically on SQLite and Postgres so retrieval stays in the unit test tier, and `matched_terms` on every result says why two incidents were judged similar, which is the difference between a citation and an assertion. The honest limit is that it matches wording rather than meaning, and 2.8 revisits that with the graph. Only RECOVERED incidents are retrievable: one still in flight has no outcome to learn from and an escalated one has no fix to reuse, so offering either would be offering a guess as experience. Every result carries a structured `Citation` and `resolve_citation` raises rather than returning a placeholder, because a reference to a record that does not exist launders a guess into evidence. Retrieval is flag-gated (`HELIX_RETRIEVAL_ENABLED`, default off) so its effect is measured rather than assumed, and a retrieval failure is swallowed to empty context rather than failing the run - an aid that can fail a run that would otherwise have succeeded measures fragility, not usefulness. Arm C composes exactly the same nodes as arm B with retrieval on, so a C-vs-B comparison isolates what RAG buys over a bare LLM instead of confounding it with validation or verification.

**D22. The graph reads a canonical event when an incident has no signal row.** E4 could open incidents from events and E5 could emit them, but nothing bridged the two: `agent/graph.py`'s intake node built its diagnosis input solely from `state.models.Signal`, which `events.ingest.ingest_event` never writes. An event-sourced incident therefore reached the graph with an empty signal and could not be diagnosed at all - a hole neither E4 nor E5 surfaced, because neither ever drove the graph from one. `_signal_from_events` closes it: when `incident.signals` is empty, the intake node reconstructs the signal view from the most recent non-Helix `EventRecord` envelope, reading `source` from the column and `signature`/`raw` from `metadata` only where present rather than defaulting absent fields to empty. It fires only when there is no signal row, so the direct path never reaches it and Phase 1 behaviour is unchanged. This was found while building the E7 event-path benchmark, where the first implementation hid it by having the harness backfill a Signal row - which made the direct-vs-event comparison a comparison of the direct path against itself.

**D23. The benchmark stays on kind. A cloud deployment is deferred, not dropped, and now has a stub.** `infra/gcp/README.md` scopes the full kind-to-GKE delta and `scripts/gcp-up.sh` is its entry point - it refuses to run and prints the prerequisites, because a stub that half-creates cloud resources bills and leaves state nobody knows about. Deliberately no Terraform: an unapplied `google_container_cluster` would look like progress and prove nothing, since the first real `apply` finds every mistake in it anyway. DJS23OLOE501 carries a cloud requirement, so GCP is outstanding scope rather than a rejected option - this records it so the question does not go missing again, having sat unanswered from 1.2 through 1.5 while the work proceeded on kind regardless. Nothing in the repo depends on GCP today: the only reference is a comment in `sandbox/payment-api/k8s/06-ingress.yaml` noting that a GKE move replaces that one file. plan.md assumption 1 therefore stands unretired.

Keeping the measured work local is the point. The benchmark rebuilds cluster state on every run, which is free and instant on kind and minutes of real billing on GKE; and the 1.11 tolerance criterion needs three consecutive runs inside a band, which a GKE auto-upgrade mid-series would silently turn into a measurement of GKE. When the requirement is addressed, the cheaper shape is a one-off GKE deployment of `payment-api` as a separate artifact for the report, with the benchmark staying local - the manifests are already isolated for exactly that, and it keeps the results reproducible.

### Input still needed

- Is there a hard submission deadline for DJS23OLOE501 that should compress Phase 2 scope? The plan does not block on this: if the schedule tightens, cut in the order given under Scope Compression Order below.

---

# Phase 1 - Evidence-Constrained DevOps Recovery

**Phase goal:** an incident arrives, Helix collects real evidence, forms a hypothesis, picks a bounded action, validates it, executes or proposes it, and verifies recovery. Useful and demoable with zero Phase 2 integrations.

**Phase exit gate:** the controlled benchmark (subphase 1.11) runs unattended across all scenarios and reports RCA accuracy, action validity, recovery success, MTTR, and unsafe action rate.

---

## 1.1 - Foundations and Repository Skeleton

**Goal:** a runnable, testable, lintable skeleton with nothing clever in it.

**Deliverables**

- Directory layout per CONTEXT.md §21 (`agent/`, `integrations/`, `state/`, `tests/`, `scripts/`, `infra/`, `docs/`).
- Dependency and tooling setup: package manager, formatter, linter, test runner.
- Configuration and secret loading. Secrets from environment or a secret file, never committed.
- Structured logging with an incident-scoped correlation field.
- Agent observability: metrics and tracing across workflow steps, LLM calls, and evidence collection. Phase 1.5 latency measurement has no source without this.
- LLM access behind a thin interface with a pinned model version, fixed temperature, and a fixed seed where the provider supports one. This determinism policy lives here because every evaluation subphase depends on it.
- CI pipeline that runs lint and tests on every push.

**Exit criterion:** `lint` and `test` both pass locally and in CI on an empty test suite plus one smoke test.

**Not in this subphase:** any integration, any agent, any database schema.

---

## 1.2 - Sandbox Target Environment

**Goal:** a system that can be broken on purpose, the same way every time. This must exist before diagnosis has anything to diagnose.

**Deliverables**

- Demo service (`payment-api`) with a Dockerfile and a test suite.
- Kubernetes manifests: deployment, service, config, secret, ingress.
- Jenkins running in Docker on the host, outside the cluster, per decision D1.
- Jenkins pipeline: build, test, containerize, deploy.
- ELK stack receiving application and cluster logs.
- Fault injection catalogue: a scripted way to trigger each benchmark failure mode (dependency failure, test regression, Docker build failure, config mismatch, CrashLoopBackOff, OOMKilled, failed rollout, missing Secret, bad deployment config).
- `scripts/` entries to bring the whole environment up and tear it down.

**Exit criterion:** every fault in the catalogue can be triggered by one command, produces the expected failure signal, and can be reset to a known-good state.

**Not in this subphase:** Helix consuming any of it. Terraform and Ansible formalization, and the SonarQube quality gate, which all land in 1.13.

---

## 1.3 - Evidence Layer (Read-Only Capability Interfaces)

**Goal:** agents can obtain facts without ever touching a vendor SDK.

**Deliverables**

- Capability interfaces defining what evidence Helix can request, expressed in Helix vocabulary, not vendor vocabulary.
- `integrations/github/` - commits, diffs, PRs, checks.
- `integrations/jenkins/` - builds, stages, logs, test results.
- `integrations/kubernetes/` - resources, pod status, events.
- `integrations/docker/` - container and image state.
- `integrations/elk/` - bounded log queries by service, time window, and level.
- Recorded fixtures captured from the 1.2 sandbox so tests run without live infrastructure.
- Secret redaction applied inside the capability layer, before evidence reaches agent context or an LLM prompt: credentials, tokens, keys, and connection strings stripped from logs, manifests, and environment dumps. Redaction is the evidence layer's job, never the caller's.
- Uniform handling of the boring failures: auth failure, timeout, rate limit, resource not found, partial data.

**Exit criterion:** each capability returns normalized evidence against both live sandbox and recorded fixtures, and every listed failure mode has a test. A fixture carrying a planted secret is provably redacted before it leaves the evidence layer.

**Not in this subphase:** any write operation. Any LLM call.

---

## 1.4 - Incident State and Intake

**Goal:** a failure signal becomes a durable incident record.

**Deliverables**

- PostgreSQL schema for the canonical active state of CONTEXT.md §13: incident, status, service, environment, severity, attempt count, linked PR and deployment.
- Migration tooling.
- Incident state machine with explicit, recorded transitions.
- Intake endpoint receiving Jenkins and Kubernetes failure signals.
- Deduplication: repeated signals for the same underlying failure attach to the existing incident instead of creating a new one.

**Exit criterion:** triggering the same sandbox fault three times in quick succession produces one incident with three attached signals, and every state transition is persisted with a timestamp and a cause.

**Not in this subphase:** Kafka. Any event bus. Direct call from intake into the agent is correct here.

---

## 1.5 - Coordinator and Diagnosis Agent

**Goal:** evidence in, root-cause hypothesis out, with the evidence chain retained.

**Deliverables**

- LangGraph workflow skeleton and typed workflow state (`agent/graph.py`, `agent/state.py`).
- Coordinator node owning state transitions and deciding the next investigation step.
- Diagnosis Agent that selects which evidence to collect, collects it through 1.3 capabilities, and interprets it.
- Hypothesis record: claim, supporting evidence references, confidence, contradicting signals.
- Bounded evidence collection so investigation cannot loop or run away.

**Exit criterion:** on at least three distinct sandbox faults, Helix produces a hypothesis whose cited evidence actually supports it, and the full evidence chain is reconstructable from stored state.

**Not in this subphase:** proposing or taking any action.

---

## 1.6 - Recovery Planner

**Goal:** candidate actions, ranked, with reasons.

**Deliverables**

- Bounded action catalogue. Each action declares its parameters, risk tier, preconditions, expected effect, and whether it is reversible together with its compensating action. Typical Phase 1 set: restart pod, roll back deployment, scale, correct configuration via PR, re-run pipeline.
- Planner producing multiple candidates with a rationale referencing the hypothesis evidence.
- Ranking over evidence support, risk tier, expected effect, and blast radius.
- Explicit "no safe action, escalate" outcome.

**Exit criterion:** for each benchmark scenario the planner emits a ranked candidate list, the top candidate is in the scenario's valid-action set, and no unsafe action is ranked first.

**Not in this subphase:** executing anything. Validation.

---

## 1.7 - Validation Agent

**Goal:** check the selected action before it touches the system, when checking is practical.

**Deliverables**

- Validation strategies per action type: Kubernetes dry-run, manifest diff, config schema check, targeted test run, container build in isolation.
- Honest "not validatable" result rather than a fabricated pass.
- Validation outcome feeding back into the Coordinator.

**Exit criterion:** a deliberately wrong candidate action fails validation and triggers replanning rather than execution.

**Not in this subphase:** the replanning loop itself beyond a single retry hook (that is 1.10).

---

## 1.8 - Safety Policy and Execution

**Goal:** actions execute inside enforced boundaries, and risky ones stop for a human.

**Deliverables**

- Policy engine mapping action plus environment plus risk tier to allow, allow-with-checks, require-approval, or deny, matching the access table in the source documents.
- Controlled write integrations: Kubernetes low-risk runtime actions in non-production; GitHub branch, commit, and PR for persistent fixes.
- Approval gate that suspends the workflow and persists the pending decision. Phase 1 surface is CLI plus API; Slack arrives in Phase 2.
- Execution audit record: who, what, when, under which policy decision.
- Hard constraints: no arbitrary shell exposed to the LLM, no direct push to protected branches, no production mutation without approval.

**Exit criterion:** a production-tier action halts awaiting approval and cannot proceed without it; a low-risk staging action executes and is fully audited; an attempt to execute outside the action catalogue is rejected.

**Not in this subphase:** auto-merging PRs. Any production write path beyond the approval gate.

---

## 1.9 - Verification Agent and Decision Provenance

**Goal:** prove the system actually recovered, and make the whole decision auditable.

**Deliverables**

- Verification Agent checking post-action desired state: pod health, rollout status, pipeline result, error rate in ELK.
- Selenium end-to-end check against the deployed demo service as a functional verification signal, so recovery means the service actually works rather than merely that a pod is running.
- Verification window and stability check so a pod that restarts and immediately crashes again is not scored as recovered.
- Provenance record covering trigger, evidence, hypothesis, candidates, selected action, validation, execution, outcome (CONTEXT.md §15).
- Incident closure or escalation based on the verification result.

**Exit criterion:** a successful recovery closes the incident while a flapping or failed recovery does not, a pod that is healthy but serving errors is not scored as recovered, and the provenance chain for any incident can be exported in full.

**Not in this subphase:** UI rendering of provenance (that is 1.12).

---

## 1.10 - Unhappy Paths

**Goal:** the failure modes of the agent itself are first-class, per CONTEXT.md §14.

**Deliverables**

- Replanning on failed validation or failed verification, with the failed attempt excluded from reconsideration.
- Compensating action after failed verification: a reversible executed action is undone using the compensating action declared in the 1.6 catalogue before replanning begins. An irreversible action escalates instead of retrying.
- Repeat-action prevention.
- Bounded retries per incident and overall.
- Escalation on low confidence, exhausted retries, or absent safe action.
- Timeout and stall handling for every long-running step.
- Concurrency safety: duplicate signals and concurrent workflow runs on one incident cannot double-execute an action.

**Exit criterion:** tests cover each transition in the CONTEXT.md §14 unhappy path sequence, including rejected validation, retry exhaustion, and escalation.

**Not in this subphase:** none. This subphase exists specifically so these are not an afterthought.

---

## 1.11 - Controlled Benchmark and Evaluation

**Goal:** measured results, not anecdotes.

**Deliverables**

- Roughly 12 reproducible scenarios covering the CI/CD, Kubernetes, and GitHub categories of CONTEXT.md §18. The organizational and cross-system categories add about 7 more in Phase 2, reaching the 15 to 20 total that CONTEXT.md §18 requires.
- Each scenario declares root cause, valid actions, unsafe actions, optimal action, expected final state.
- Scripted approver implementing decision D2: approval granted only where the scenario declares the optimal action to be approval-gated, that approval recorded as its own outcome class, and any action executed without a required approval scored unsafe whatever its result.
- Automated harness that resets the sandbox, injects the fault, runs Helix, and scores the outcome.
- Metrics: RCA accuracy, action validity, recovery success, MTTR, unsafe action rate.
- Ablation arms A (rules only) and B (single LLM agent) as baselines against arm E (full Helix).

**Exit criterion:** a single command produces a scored report over the full scenario set, and three consecutive runs stay inside a documented tolerance band on every metric. Variance beyond that band is a defect, since 1.1 fixed the determinism policy.

**Not in this subphase:** ablation arm C, which needs the retrieval layer built in E6, and arm D, which needs graph context from 2.7.

---

## 1.12 - Operator Interface

**Goal:** a human can watch and understand an incident without reading the database.

**Deliverables**

- `ui/` application serving the operator interface.
- Incident list and incident detail view.
- Provenance timeline: trigger, evidence, hypothesis, candidates, decision, validation, execution, verification.
- Approval queue with approve and reject.
- Live status of an in-flight investigation.

**Exit criterion:** a full incident, including an approval, can be followed start to finish through the UI alone, with no visual defects on the paths exercised.

**Not in this subphase:** dashboards, analytics, multi-user roles.

---

## 1.13 - Infrastructure Formalization and Quality Gates

**Goal:** the environment is reproducible from code, and the repo holds its own quality bar.

**Deliverables**

- Terraform for infrastructure provisioning.
- Ansible for configuration and node setup.
- SonarQube quality gate wired into the Jenkins pipeline.
- Ingress exposing both the demo service and the Helix interface.
- Documented one-command bring-up from a clean machine.

**Exit criterion:** the entire environment is rebuilt from scratch on a clean machine using only committed code and documented steps.

**Not in this subphase:** multi-environment or production-grade infrastructure.

---

# Phase 1.5 - Event-Driven Helix

**Phase goal:** decouple event production from agent reasoning, normalize events, and preserve history, without regressing what Phase 1 does. The single behavioral addition is E6 retrieval, and it stays flag-gated so its effect is measured rather than assumed.

**Entry gate:** Phase 1 exit gate met. Kafka is introduced because integrations and asynchronous workflows now demand it, not for the diagram.

**Phase exit gate:** Phase 1 benchmark results are reproduced through the event path with no regression, plus correlation, latency, and replay metrics, and ablation arm C added to the table.

---

## E1 - Canonical EngineeringEvent Model

**Goal:** one event shape, defined and validated, before any broker exists.

**Deliverables**

- `events/schemas.py` implementing the canonical envelope from CONTEXT.md §7.
- The small starting taxonomy from CONTEXT.md §7, and nothing beyond it. Reconcile the one discrepancy between the source documents before coding: the idea document §7 lists `rollout.failed` and `service.unhealthy` in the runtime family, while CONTEXT.md §7 omits both. Decide once, record the decision, and keep the taxonomy single-sourced.
- Schema validation, versioning field, and a documented compatibility rule.
- Phase 1 intake refactored to construct canonical events internally and pass them in process.

**Exit criterion:** Phase 1 benchmark passes unchanged with canonical events flowing through the existing direct path.

**Not in this subphase:** Kafka.

---

## E2 - Kafka Backbone

**Goal:** the transport, with retention and operational basics.

**Deliverables**

- Kafka in the infrastructure stack.
- Topic design, partitioning keyed for correlation, and retention policy.
- Producer and consumer wrappers with at-least-once delivery, consumer groups, and offset handling.
- Dead letter topic and poison message handling.

**Exit criterion:** events produced by a test harness are consumed reliably; broker restart loses nothing; a malformed event lands in the dead letter topic instead of stalling the consumer.

**Not in this subphase:** rewiring the real integrations.

---

## E3 - Event Adapters

**Goal:** external systems publish canonical events rather than calling Helix.

**Deliverables**

- `events/adapters/` for GitHub, Jenkins, Kubernetes, and ELK.
- Adapters translate vendor payloads into canonical events and own all vendor-specific knowledge.
- Idempotency: a redelivered vendor webhook yields the same event id.

**Exit criterion:** every Phase 1 failure signal now arrives as a canonical event on Kafka, and no vendor payload shape reaches an agent.

**Not in this subphase:** new event types beyond the taxonomy.

---

## E4 - Correlation and Consumption

**Goal:** related events across systems collapse into one incident.

**Deliverables**

- Consumer service driving Helix state from the event stream.
- Correlation engine linking events by service, environment, commit, PR, build, and deployment into a shared `correlation_id`.
- Duplicate and out-of-order handling.
- Idempotent state transitions so replayed events do not re-execute actions.

**Exit criterion:** a scenario spanning GitHub, Jenkins, Kubernetes, and ELK produces one incident with all four sources correlated; replaying the same events changes nothing.

**Not in this subphase:** graph-based correlation, which is Phase 2.

---

## E5 - Helix Event Emission and History

**Goal:** Helix becomes an event producer, and history becomes queryable.

**Deliverables**

- Emission of `incident.created`, `remediation.requested`, `approval.requested`, `remediation.completed`, `incident.resolved`.
- Durable event history store.
- Replay: rebuild incident state from the event log.
- Event history query surface for later use by the Knowledge Agent.

**Exit criterion:** incident state rebuilt purely from replayed history matches the state produced by live processing.

**Not in this subphase:** consumers of Helix events. Phase 2 supplies those.

---

## E6 - Retrieval Over Event and Incident History

**Goal:** past incidents become usable context, without a graph. This is the subphase that makes ablation arm C buildable.

**Deliverables**

- Retrieval layer over the E5 event history, stored incidents, and their provenance records.
- Indexing and similarity search across incident summaries, hypotheses, actions, and outcomes.
- Every retrieval result carries a citation back to the source incident or event.
- Prior-incident context offered to the Diagnosis Agent behind a flag, so its effect is measured rather than assumed.

**Exit criterion:** for a scenario resembling a previously resolved incident, retrieval returns that incident with a citation resolving to a real record, and ablation arm C runs end to end.

**Not in this subphase:** graph traversal and GraphRAG, which compose with this layer in 2.8.

---

## E7 - Phase 1.5 Evaluation

**Goal:** measure what the event layer bought.

**Deliverables**

- Event correlation accuracy, duplicate handling rate, end-to-end event latency, and replay correctness measurements.
- Full Phase 1 benchmark rerun through the event path.
- Comparison of direct-path and event-path results.
- Ablation arm C (LLM plus RAG) measured against arms A, B, and E from 1.11.

**Exit criterion:** no regression against Phase 1 numbers, correlation and replay metrics recorded, and arm C added to the ablation table.

**Not in this subphase:** ablation arm D, which needs the graph from 2.7, and any Phase 2 metric.

---

# Phase 2 - Agentic Engineering Lifecycle Platform

**Phase goal:** extend, never replace, the Phase 1 recovery core with triage, review, knowledge, and human and organizational systems.

**Entry gate:** Phase 1.5 exit gate met.

**Phase exit gate:** the full lifecycle of CONTEXT.md §16 runs end to end, scored against the expanded benchmark.

---

## 2.1 - Ops Agent Consolidation and State Extension

**Goal:** absorb the Phase 1 recovery loop into the Phase 2 shape without changing its behavior.

**Deliverables**

- Phase 1 Diagnosis, Planner, Validation, and Verification consolidated under the Ops Agent boundary, with internals unchanged.
- State model extended with provider-neutral concepts: `WorkItem`, `Status`, `Assignee`, `Priority`, `Project`, `Link`, `Comment` (CONTEXT.md §11).
- Ownership and service registry primitives.
- Multi-agent Coordinator handling agent handoff, ordering, and duplicate-action prevention across agents.

**Exit criterion:** the Phase 1 benchmark passes unchanged after consolidation. This is a pure structural move.

**Not in this subphase:** any new agent behavior.

---

## 2.2 - Slack Integration

**Goal:** humans participate in the loop where they belong: communication, approval, escalation.

**Deliverables**

- `integrations/slack/` for notification, threaded incident updates, interactive approval, and escalation.
- Approval gate from 1.8 given a Slack surface alongside the existing CLI and API surfaces.
- Approval identity and authorization checks.
- Timeout and fallback when nobody responds.

**Exit criterion:** a production-tier action requests approval in Slack, an authorized approval resumes the workflow, a rejection triggers replanning, and a timeout escalates. Slack outage does not lose the decision, because Slack is not the database.

**Not in this subphase:** Slack as a state store. Free-form conversational control of Helix.

---

## 2.3 - Work Management Provider and Jira

**Goal:** incidents become tracked work, through a provider-neutral contract from day one.

**Deliverables**

- `WorkManagementProvider` contract: `create_work_item`, `update_work_item`, `assign_work_item`, `transition_work_item`, `comment_work_item`, `link_work_item`.
- `JiraProvider` as the first implementation.
- Incident to work item mapping and lifecycle synchronization.
- Duplicate work item prevention.

**Exit criterion:** an incident creates exactly one Jira issue, keeps it in step through the incident lifecycle, and closes it on resolution. No Jira-specific vocabulary appears in any agent.

**Not in this subphase:** Linear and Trello, which land in 2.9.

---

## 2.4 - Notion Knowledge Integration

**Goal:** durable, human-readable incident knowledge.

**Deliverables**

- `integrations/notion/` creating and updating incident pages.
- Incident page template: timeline, evidence, hypothesis, actions taken, outcome, follow-ups.
- Update on significant state transitions rather than on every event.

**Exit criterion:** a completed incident has a Notion page containing the full provenance chain, updated through the incident lifecycle, with no duplicate pages.

**Not in this subphase:** Notion as a source of truth or as a queryable knowledge store. It is a projection of PostgreSQL state, per CONTEXT.md §13. Querying organizational knowledge is 2.8.

---

## 2.5 - Triage Agent

**Goal:** what happened, what is affected, who owns it.

**Deliverables**

- Impact analysis: affected service, environment, blast radius, severity.
- Ownership resolution from code ownership, commit history, and the service registry.
- Duplicate incident detection against event history.
- Missing-owner path: escalate rather than guess.

**Exit criterion:** on the organizational benchmark scenarios, Triage identifies the affected service and owner correctly, detects duplicates, and escalates cleanly when ownership is genuinely unknown.

**Not in this subphase:** graph-backed ownership, which arrives in 2.7.

---

## 2.6 - Review Agent

**Goal:** who should review this change, and why.

**Deliverables**

- Reviewer selection from ownership, review history, recency, and current load.
- Handling of the case where the owner authored the change.
- Review request delivered through Slack with the reasoning attached.
- Review outcome tracked back into incident and work item state.

**Exit criterion:** on the GitHub benchmark scenarios, a reviewer is selected with a stated justification, self-review is never proposed, and a rejected review drives the `REJECTED -> FIX_REQUIRED -> REVIEW_PENDING` path.

**Not in this subphase:** graph-backed review history, which arrives in 2.7. Approving or merging a pull request on a human's behalf, in any circumstance.

---

## 2.7 - Neo4j Relationship Graph

**Goal:** relationship questions that the relational state cannot answer cheaply.

**Deliverables**

- `graph/` module holding the schema from CONTEXT.md §12: developer, service, repository, database, PR, deployment, incident, work item, knowledge page, and the relationships between them.
- Ingestion from GitHub, event history, and PostgreSQL state.
- Incremental update from the event stream.
- Query layer answering the CONTEXT.md §12 questions.
- Triage and Review switched to graph-backed context, with their pre-graph results kept as the comparison baseline.

**Exit criterion:** all CONTEXT.md §12 example questions are answered correctly against the sandbox, and owner and reviewer accuracy is measured with and without the graph.

**Not in this subphase:** GraphRAG retrieval.

---

## 2.8 - Knowledge Agent and GraphRAG

**Goal:** what the organization already knows about this system.

**Deliverables**

- GraphRAG retrieval combining graph traversal with the E6 retrieval layer, rather than a second retrieval stack.
- Knowledge Agent answering prior-incident, prior-fix, and dependency questions.
- Knowledge context injected into diagnosis and planning.
- Provenance for retrieved knowledge, so a claim can be traced to a source.

**Exit criterion:** on a scenario resembling a previously resolved incident, the Knowledge Agent surfaces the prior incident and its fix, and the citation resolves to a real record.

**Not in this subphase:** a second retrieval stack alongside E6. Knowledge output that changes a recovery decision without a citation a human can follow.

---

## 2.9 - Full Lifecycle Orchestration

**Goal:** the CONTEXT.md §16 flow, unbroken, across every integration.

**Deliverables**

- End-to-end orchestration: PR opened, triage, reviewer selection, Slack request, pipeline failure, Ops evidence collection, Jira and Notion records, approval, fix PR, validation, verification, resolution, final notification.
- Cross-system consistency reconciliation for Jira and Notion drift.
- Lifecycle-wide unhappy paths: partial integration outage, conflicting state, stale work items.

**Exit criterion:** the cross-system benchmark scenarios complete end to end, and an induced Jira or Notion outage degrades gracefully without losing incident state.

**Not in this subphase:** new agent capability. 2.9 wires together what 2.1 through 2.8 already built and adds only reconciliation. Any capability gap found here becomes a defect against its owning subphase.

---

## 2.10 - Optional Providers and Provider Study

**Goal:** prove the provider contract is real by swapping the provider.

**Deliverables**

- `LinearProvider` and `TrelloProvider` behind the existing contract.
- Provider selected by configuration.
- Capability mapping per the source document table, with explicit handling of capabilities a provider lacks.
- Provider study: the same workflow run on Jira, Linear, and Trello.

**Exit criterion:** identical lifecycle outcomes across all three providers with zero changes to agent code, evaluated within each provider's declared capability set. Where a provider genuinely lacks a capability, such as Trello having no workflow state beyond a list and no native priority, the degradation is declared in its capability map and asserted by a test. An undeclared behavioral difference is a defect in the abstraction; a declared one is a finding for the study.

**Not in this subphase:** provider-specific workflow logic anywhere inside an agent. A provider that cannot be added without touching agent code is a failed exit criterion here and a defect in 2.3, not a reason to widen this subphase.

---

## 2.11 - Phase 2 Evaluation and Ablation

**Goal:** measure whether the added architecture actually helped.

**Deliverables**

- Benchmark expanded from the roughly 12 Phase 1 scenarios to the full 15 to 20 across all five CONTEXT.md §18 categories, adding organizational and cross-system.
- Metrics: owner identification accuracy, reviewer selection accuracy, lifecycle consistency, duplicate work item rate, decision evidence coverage, human intervention rate.
- Complete ablation from CONTEXT.md §19: A rules only, B single LLM agent, C LLM plus RAG, D event plus graph context, E full Helix plus verification. Arms A, B, and E come from 1.11 and arm C from E7; 2.11 adds arm D and reruns all five across the full benchmark.
- Written analysis, including any case where more architecture did not improve results.

**Exit criterion:** the full ablation table is populated across the full benchmark, and the analysis states plainly which components earned their place.

**Not in this subphase:** new capability of any kind. If a measurement exposes a gap, it becomes a follow-up subphase with its own exit criterion rather than unscoped work inside 2.11.

---

# Optional Extension

## OPT-1 - MLOps Extension (PyCaret)

Optional, and only after 2.11. It exists to cover DevOps Laboratory Unit 10 and is not part of the Helix core thesis.

**Goal:** demonstrate an MLOps workflow over data Helix already holds.

**Deliverables**

- PyCaret pipeline trained on accumulated incident and event history, predicting one narrow, useful target such as incident severity or likelihood of recurrence after a given action.
- Training, evaluation, and model versioning run through the existing Jenkins pipeline.
- Model output surfaced as advisory context only.

**Exit criterion:** the pipeline trains and evaluates reproducibly through Jenkins, and reports honest accuracy against a held-out set, including when the accuracy is poor.

**Not in this subphase:** any model output that gates, ranks, or selects a recovery action. An advisory signal must not become an unaudited decision path.

---

# Cross-Cutting Rules

These hold in every subphase.

| Rule | Source |
|---|---|
| Phase 1 is useful without Phase 2. Phase 2 extends, never replaces. | CONTEXT.md §22 |
| Add an abstraction only when a repeated, concrete need appears. | CONTEXT.md §22 |
| No integration added for the architecture diagram alone. | CONTEXT.md §22 |
| Persistent code and configuration changes go through a GitHub PR. | CONTEXT.md §22 |
| Least privilege. High-risk production actions need human approval. | CONTEXT.md §22 |
| No arbitrary shell access exposed to the LLM. | CONTEXT.md §22 |
| Vendor APIs never enter agent reasoning. Capability interfaces only. | CONTEXT.md §22 |
| Small number of reliable agents over a large swarm. | CONTEXT.md §22 |
| Reproduce bugs end to end before fixing. Lint failures, test failures, and flakiness are defects. | CONTEXT.md §22 |
| No em dash characters anywhere. | CONTEXT.md §23 |

## Explicitly Deferred

Building any of these earlier than its phase is a plan violation, not an optimization.

| Item | Earliest phase |
|---|---|
| Kafka and any event bus | Phase 1.5 (E2) |
| Neo4j and GraphRAG | Phase 2 (2.7, 2.8) |
| Slack, Jira, Notion | Phase 2 (2.2, 2.3, 2.4) |
| Linear and Trello | Phase 2 (2.10) |
| MCP or a general tool-control layer | Only on a demonstrated reuse or decoupling need |
| Triage, Review, and Knowledge agents | Phase 2 |
| Multi-provider LLM abstraction | Only when a second provider is actually required |
| Retrieval, RAG, or similarity search over history | Phase 1.5 (E6), and only once E5 has history to retrieve |
| PyCaret and any ML model | OPT-1, optional and only after 2.11 |

## DevOps Laboratory Unit Traceability

The idea document §18 maps ten graded lab units (DJS23OLOE501). Each has a home. Review this table at every phase gate, because a plan can execute perfectly and still miss an assessed unit.

| Unit | Lab topic | Covered by |
|---|---|---|
| 1 | Docker | 1.2 containerization, 1.3 container evidence |
| 2 | Terraform | 1.13 |
| 3 | Puppet / Ansible | 1.13 |
| 4 | Jenkins | 1.2 pipeline, 1.13 quality gate and secret handling |
| 5 | Selenium | 1.9 end-to-end verification signal |
| 6 | Scripting | 1.2 fault injection and environment scripts, 1.13 bring-up |
| 7 | Kubernetes | 1.2 manifests, 1.3 state evidence, 1.8 controlled remediation |
| 8 | Ingress | 1.13 |
| 9 | ELK | 1.2 stack, 1.3 log evidence |
| 10 | PyCaret | OPT-1, optional |

## Scope Compression Order

If the schedule tightens, cut from the top of this list. Each entry says what is actually lost, because a cut made without knowing that is a cut made blind.

| Order | Cut | What is lost |
|---|---|---|
| 1 | OPT-1 PyCaret | DevOps Laboratory Unit 10 coverage. Nothing in the Helix thesis. |
| 2 | 2.10 Linear and Trello | The provider study as evidence. The provider contract itself is already proven by 2.3, so the architecture claim survives; only the demonstration weakens. |
| 3 | 2.8 GraphRAG depth | Organizational knowledge retrieval. Keep 2.7, since arm D depends on the graph and ablation collapses without it. |
| 4 | Ablation arms C and D | The measurement of whether retrieval and graph context earned their place. Arms A, B, and E still answer the core question. |
| 5 | 2.9 cross-system reconciliation | Graceful degradation under integration outage. Accept it only if Phase 2 is being demoed rather than evaluated. |

**Never cut:** 1.8 safety policy, 1.10 unhappy paths, 1.11 benchmark. These are what separate Helix from a script that restarts pods, and CONTEXT.md §22 treats every one of them as non-negotiable.

## Principal Risks

| Risk | Where it bites | Mitigation |
|---|---|---|
| Sandbox faults are not reproducible | 1.2, invalidates every benchmark number | Faults scripted and reset-verified before 1.3 starts |
| Diagnosis hypotheses that sound right but cite nothing | 1.5 | Hypothesis requires evidence references; unsupported claims fail the exit criterion |
| Agent executes an unsafe action | 1.8 | Bounded action catalogue plus policy engine plus approval gate; unsafe action rate is a first-class metric |
| Kafka added without a real need | E2 | Gated behind the Phase 1 exit gate and behind E1 proving the canonical model first |
| Phase 2 quietly rewrites Phase 1 | 2.1 | 2.1 is a pure structural move whose exit criterion is an unchanged Phase 1 benchmark |
| Provider abstraction leaks Jira semantics | 2.3, surfaces at 2.10 | Contract defined before Jira exists; the 2.10 study is the proof |
| Retry loops that never terminate | 1.10 | Bounded retries and escalation are their own subphase, not a late patch |
| Secrets in logs and manifests reach an LLM prompt | 1.3, and every subphase downstream of it | Redaction sits inside the evidence layer and is asserted by a planted-secret test |
| An executed action cannot be undone after failed verification | 1.9 into 1.10 | Actions declare reversibility in the 1.6 catalogue; irreversible actions escalate rather than retry |
| LLM non-determinism makes benchmark numbers unreproducible | 1.11, and every later evaluation | Pinned model, temperature, and seed from 1.1, plus a documented tolerance band over three runs |
| Graded lab units left uncovered by the core plan | OPT-1 and 1.13 | Explicit lab unit traceability table, reviewed at each phase gate |
