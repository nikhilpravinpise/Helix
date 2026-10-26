# Helix: Repository-Grounded Capability Fact Sheet

*Scope: this note is built only from the local repository at `C:\Dev\Helix`, HEAD `b1a5ccc5abfe78806d9d448da7f5e43d98fc6ff4` (2026-10-19, branch `main`, 295 commits). No web research was used for any Helix claim. External CodeRabbit facts are taken only from the sibling note `landscape_evidence.md` and are marked as such. Citations are file paths with line ranges; `path:lines` links point at the local checkout.*

*Reading convention: **Cited Findings** are direct repository facts. **Inferences** are my interpretation. **Gaps** are things the repository does not establish, or that I could not verify locally.*

---

## 1. What is Helix, and what end-to-end loop does it actually implement?

### Takeaway
Helix is a single-team "agentic DevOps" web product with two distinct loops sharing one state machine and one audit store: an **incident-recovery loop** (diagnose → plan → validate → policy → execute → verify, with compensation) and a **change-review loop** (triage → owner → reviewer → approve/reject → fix → deploy → hand failed deploy to recovery). Its "review" is **human reviewer routing and a binary approval gate**, not automated code critique. Postgres is the source of truth; Slack, Jira, Notion and Neo4j are projections fed by a transactional outbox.

### Cited Findings
- Helix describes itself as "an agentic DevOps system that handles two kinds of engineering work on its own, and asks a human at the points that matter": recovery and change review — [README.md:3-12](C:/Dev/Helix/README.md#L3-L12).
- The change loop is stated as: "A pull request opens. Helix triages it, picks the owner and a reviewer from the service registry, follows review, rework and deploy, and hands a failed deploy to the recovery loop." — [README.md:10-12](C:/Dev/Helix/README.md#L10-L12).
- Postgres is the source of truth; "Slack, Jira, Notion and a Neo4j graph are projections of it, fed through a transactional outbox." — [README.md:14-15](C:/Dev/Helix/README.md#L14-L15).
- The recovery loop is `diagnose -> plan -> validate -> policy -> execute -> verify`; the change loop is `triage -> owner -> review -> deploy` — [README.md:32-34](C:/Dev/Helix/README.md#L32-L34).
- Status is claimed as "Phase 1, Phase 1.5, Phase 2 and the optional OPT-1 are done (32 of 32 subphases)" — [README.md:17-21](C:/Dev/Helix/README.md#L17-L21).
- The system is deployed as a hosted product at `https://helix-eksf.onrender.com`, running "on recorded evidence with the rules arm only: no cluster, Kafka, Jenkins or LLM key" — [README.md:103-110](C:/Dev/Helix/README.md#L103-L110).
- The hosted app is described as "the product" for another team, with sign-in, Slack/Jira/GitHub connections, people/services management and dashboard/Slack approvals — [HANDOFF-NIKHIL.md:23-29](C:/Dev/Helix/HANDOFF-NIKHIL.md#L23-L29).
- Repository scale at HEAD: 539 tracked files, 327 Python files, 43 Markdown files; 188 non-test Python files (31,843 non-blank lines) and 139 test-related Python files, of which the 135 under `tests/` total 30,506 non-blank lines (counts from `git ls-files`; line counts exclude blank lines).
- The state machine defines 17 statuses and "36 workflow edges plus the blanket rule that any non-terminal status can go to `escalated` or `failed`" — [docs/architecture/state-machine.md:36-58](C:/Dev/Helix/docs/architecture/state-machine.md#L36-L58); the enum itself is at [state/machine.py:38-62](C:/Dev/Helix/state/machine.py#L38-L62).

### Inferences
- Helix is best understood as **workflow and governance infrastructure around changes and incidents**, not as a code-analysis product. The change loop's outputs are an assigned human, a durable decision row, and a deployment hand-off — not review findings.
- The two loops are genuinely coupled: `deploying` can transition to `diagnosing` on a failed deploy, so the change loop can become an incident without a new human request — [docs/architecture/state-machine.md:55-56](C:/Dev/Helix/docs/architecture/state-machine.md#L55-L56).
- The hosted product is materially narrower than the repository: Render runs fixtures plus arm A only, so most of the LLM, cluster and pipeline behaviour is demonstrated by the benchmark rather than by the live URL — [render.yaml:66-81](C:/Dev/Helix/render.yaml#L66-L81).

### Gaps
- "32 of 32 subphases" is a project-planning claim, not independently verified against a requirements document in this note; I did not audit every subphase.
- The repository has no `LICENSE` file at HEAD, so its distribution terms cannot be determined from the checkout.

---

## 2. How does the change/PR review and approval flow work, exactly?

### Takeaway
A GitHub PR (or a polled PR, or a benchmark event) opens a `change` incident. Triage computes blast radius from changed paths, finds duplicates, and resolves an owner by CODEOWNERS-style path rules, then registry, then commit author, else escalates. Review picks the first `default_reviewers` entry that is neither the author nor a previous rejector, writes a durable `review_requests` row, and sends an interactive Slack card. Only the requested reviewer may decide; the author may not, except that an owner/admin author may self-approve from the dashboard. Owners/admins may reassign a pending review. A stale decision is refused and a timeout escalates.

### Cited Findings
- **Trigger and incident kind.** `pull_request` actions `opened`/`reopened` map to `pr.opened`, `synchronize`/`edited` to `pr.updated`; `closed` and `merged` produce no event — [events/adapters/github.py:113-118](C:/Dev/Helix/events/adapters/github.py#L113-L118), [events/adapters/github.py:121-141](C:/Dev/Helix/events/adapters/github.py#L121-L141). `PR_OPENED`, `PR_UPDATED` and `PUSH_CREATED` open `kind=change` incidents — [events/ingest.py:64](C:/Dev/Helix/events/ingest.py#L64), [events/ingest.py:286-290](C:/Dev/Helix/events/ingest.py#L286-L290).
- **Triage blast radius is deterministic and path-based:** high if any changed file is the Secret manifest; medium if any path contains `/k8s/` or more than three files changed; otherwise low — [agent/agents/triage.py:32-40](C:/Dev/Helix/agent/agents/triage.py#L32-L40). It does not read diff content for this.
- **Duplicate detection** looks for another open `change` incident for the same service and branch — [agent/agents/triage.py:43-66](C:/Dev/Helix/agent/agents/triage.py#L43-L66).
- **Owner resolution order:** CODEOWNERS-style path rules (last matching pattern wins, union across changed paths), then `default_owner`, then first of `owners`, then commit author, else `(None, "none")` and escalation — [agent/ops/registry.py:172-222](C:/Dev/Helix/agent/ops/registry.py#L172-L222); triage applies it at [agent/agents/triage.py:145-174](C:/Dev/Helix/agent/agents/triage.py#L145-L174).
- **Reviewer selection:** first candidate in the service's `default_reviewers` list excluding the author and an `exclude` set of prior rejectors; `(None, rationale)` if all are excluded, which escalates — [agent/ops/registry.py:225-254](C:/Dev/Helix/agent/ops/registry.py#L225-L254). Declined reviewers are read from `review_requests` rows with status `rejected` — [agent/agents/review.py:28-38](C:/Dev/Helix/agent/agents/review.py#L28-L38).
- **Graph fallback:** when `HELIX_GRAPH_ENABLED`, `reviewers_for` may override the registry answer; an unreachable graph falls back silently — [agent/agents/review.py:41-64](C:/Dev/Helix/agent/agents/review.py#L41-L64). The graph query orders service `REVIEWS` edges before `OWNS` edges, because listing owners first scored reviewer accuracy 0/4 — [graph/queries.py:29-59](C:/Dev/Helix/graph/queries.py#L29-L59).
- **Durable request before delivery:** the review row is written and flushed, then a Slack outbox row is enqueued in the same transaction — [agent/agents/review.py:130-146](C:/Dev/Helix/agent/agents/review.py#L130-L146). The Slack card says why this person and links the PR, with **Approve** and **Request changes** buttons — [integrations/slack/format.py:235-258](C:/Dev/Helix/integrations/slack/format.py#L235-L258).
- **Who may decide a review:** only the requested reviewer; the author is refused; an owner/admin author may pass `self_approve=True` — [state/deciders.py:105-116](C:/Dev/Helix/state/deciders.py#L105-L116). For approvals, only a service owner/approver may decide, and the author is excluded unless self-approving — [state/deciders.py:68-102](C:/Dev/Helix/state/deciders.py#L68-L102).
- **Self-approval is role- and surface-limited:** `SELF_APPROVER_ROLES = ("owner", "admin")`; the module docstring states the automatic pick never offers it, Slack never passes it, and the file registry (benchmark/tests) has no roles so it never applies there — [state/deciders.py:10-26](C:/Dev/Helix/state/deciders.py#L10-L26). The dashboard renders a distinct "Approve my own change" button and labels the result "self-approved" — [ui/templates/_decide.html:28-44](C:/Dev/Helix/ui/templates/_decide.html#L28-L44).
- **Slack cannot self-approve:** the Slack handler calls `record_decision` without `self_approve`, so the flag defaults to false — [helix/api.py:520-538](C:/Dev/Helix/helix/api.py#L520-L538), [state/reviews.py:70-79](C:/Dev/Helix/state/reviews.py#L70-L79).
- **Reassignment:** only owner/admin; the new reviewer must be on the team, must not be the author, and must not already be the reviewer; the old reviewer is then refused; a new Slack request is enqueued — [state/reviews.py:117-157](C:/Dev/Helix/state/reviews.py#L117-L157). The API maps forbidden to 403 and invalid state to 409 — [helix/api.py:1042-1073](C:/Dev/Helix/helix/api.py#L1042-L1073).
- **Timeout:** a decision older than `approval_timeout` (default 86,400 s) is refused with "the next run escalates the incident" — [state/reviews.py:92-103](C:/Dev/Helix/state/reviews.py#L92-L103), [helix/config.py:171-172](C:/Dev/Helix/helix/config.py#L171-L172). The sweep records the request rejected-by-timeout and transitions to `escalated` — [state/reviews.py:160-190](C:/Dev/Helix/state/reviews.py#L160-L190).
- **Roles:** owner (rank 3) > admin (2) > member (1); one main owner; invites expire after 7 days — [state/team.py:12](C:/Dev/Helix/state/team.py#L12), [state/team.py:33-35](C:/Dev/Helix/state/team.py#L33-L35). A partial unique index enforces exactly one main owner (migration 0018) — [HANDOFF-NIKHIL.md:221-226](C:/Dev/Helix/HANDOFF-NIKHIL.md#L221-L226).
- **Slack identity:** only a linked Slack member id maps to a person; an unlinked click becomes `slack:<id>`, which no rule accepts — [helix/api.py:724-741](C:/Dev/Helix/helix/api.py#L724-L741), [HANDOFF-NIKHIL.md:169-174](C:/Dev/Helix/HANDOFF-NIKHIL.md#L169-L174).
- **Review state is internal, not GitHub state.** Approving in Helix updates `review_requests` and resumes the workflow; it does not post a GitHub review or approval. The GitHub webhook handles only `pull_request` and `check_run` — [helix/api.py:661-662](C:/Dev/Helix/helix/api.py#L661-L662) — and the `review_events` adapter that would translate GitHub review submissions exists but is referenced only by tests, not by the webhook or watcher — [events/adapters/github.py:72-110](C:/Dev/Helix/events/adapters/github.py#L72-L110); a repository-wide search found no production call site.
- **`PR_MERGED` is defined but unused:** `EventType.PR_MERGED` exists — [events/schemas.py:112](C:/Dev/Helix/events/schemas.py#L112) — but a repository-wide search found no producer or consumer outside the enum.
- **The change lifecycle is driven by the coordinator, one stage per run, and `approved -> deploying` does not deploy anything itself:** it waits for `build.passed`/`deployment.succeeded` or `build.failed`/`deployment.failed`, and the newest outcome wins — [agent/agents/coordinator.py:357-366](C:/Dev/Helix/agent/agents/coordinator.py#L357-L366), [agent/agents/coordinator.py:700-711](C:/Dev/Helix/agent/agents/coordinator.py#L700-L711), [agent/agents/coordinator.py:773-788](C:/Dev/Helix/agent/agents/coordinator.py#L773-L788).
- **Audit:** every status change writes a `transitions` row with `from_status`, `to_status`, `cause`, `created_at`; review decisions store `decided_by`, `decided_at`, `note` — [docs/architecture/state-machine.md:60-63](C:/Dev/Helix/docs/architecture/state-machine.md#L60-L63), [state/reviews.py:109-113](C:/Dev/Helix/state/reviews.py#L109-L113).

### Inferences
- The review gate is a **binary human attestation with an optional free-text note**, not a review-comment system. "Request changes" carries no line, file, or diff annotation; the dashboard note input is a single text field (max 500 characters in the template) — [ui/templates/_decide.html:19-26](C:/Dev/Helix/ui/templates/_decide.html#L19-L26).
- Because the review is internal, a team that already reviews on GitHub would have **two parallel review states**. Helix can read GitHub PR reviews as *diagnosis evidence* — [integrations/github/client.py:168-190](C:/Dev/Helix/integrations/github/client.py#L168-L190), [agent/agents/diagnosis.py:217-221](C:/Dev/Helix/agent/agents/diagnosis.py#L217-L221) — but it does not reconcile them with its own gate.
- Reviewer selection is deterministic and explainable ("why you" is stored in `rationale`), which is a governance strength, but it has no notion of reviewer load, expertise, availability, or review history beyond prior rejection.
- The self-approval exception is the main governance weakening: an owner/admin can unilaterally approve their own change, with only a visible "self-approved" label as the control. The repository documents this as a deliberate 2026-10-16 decision — [future_plans.md:296-310](C:/Dev/Helix/future_plans.md#L296-L310).

### Gaps
- The repository does not show what happens when the *only* owner is the author and is a member (not owner/admin): triage resolves the owner, review excludes the author, and the escalation path depends on reviewer availability; I did not trace every combination.
- No evidence was found of a maximum review age alert before the 24-hour timeout, or of a notification escalation ladder beyond the timeout sweep.
- The exact interaction between a reassigned review and a previously declined reviewer list is implemented (old reviewer refused, new one decides) but not covered by an end-to-end test I verified.

---

## 3. What is the safety and execution model for actions Helix runs?

### Takeaway
The LLM never gets a shell. It can only choose from six typed actions, each with pydantic-bounded parameters and a risk tier. A pure policy function returns `ALLOW`, `ALLOW_WITH_CHECKS`, `REQUIRE_APPROVAL` or `DENY`; the verdict is enforced twice (coordinator routing and executor gate), with a third set of code-level transport bounds. High-risk, production, unknown-environment and unregistered-namespace writes require approval; evidence from another domain also escalates rather than executes.

### Cited Findings
- "The LLM never gets a shell. It chooses from a catalogue of typed actions (restart, rollback, scale, reapply a manifest, rerun a pipeline, open a fix PR), and every write passes a policy check twice: once when the coordinator routes it, and again inside the executor." — [README.md:44-47](C:/Dev/Helix/README.md#L44-L47).
- **Exactly six actions** are registered: `restart_deployment`, `rollback_deployment`, `scale_deployment`, `reapply_manifest`, `open_fix_pr`, `rerun_pipeline` — [agent/actions/catalogue.py:90-95](C:/Dev/Helix/agent/actions/catalogue.py#L90-L95), specs at [agent/actions/catalogue.py:282-392](C:/Dev/Helix/agent/actions/catalogue.py#L282-L392).
- **Parameter bounds are code, not prompt text:** `replicas` is 1–10 (scale-to-0 refused), `reapply_manifest` paths must be on a committed allowlist, fix-PR paths must be under `sandbox/payment-api/` with no traversal, and branches must match `helix/INC-*` — [agent/actions/catalogue.py:25-31](C:/Dev/Helix/agent/actions/catalogue.py#L25-L31), [agent/actions/catalogue.py:98-166](C:/Dev/Helix/agent/actions/catalogue.py#L98-L166).
- **Policy rules:** low/medium non-prod writes get `ALLOW_WITH_CHECKS` (medium additionally requires a declared `expected_effect`); any production or unknown-environment write, any high-tier action, or a cluster write to an unregistered namespace gets `REQUIRE_APPROVAL`; unknown actions, invalid params, or bound violations get `DENY` — [agent/policy.py:7-23](C:/Dev/Helix/agent/policy.py#L7-L23), [agent/policy.py:186-214](C:/Dev/Helix/agent/policy.py#L186-L214).
- **Environment semantics:** only `sandbox` and `staging` are non-prod; anything else fails safe into approval — [agent/policy.py:49-51](C:/Dev/Helix/agent/policy.py#L49-L51), [agent/policy.py:208-214](C:/Dev/Helix/agent/policy.py#L208-L214).
- **Evidence coherence:** a runtime write must be grounded in evidence from its executor's domain. Kubernetes writes need `deployment.*`/`pod.failed`/`service.unhealthy`; Jenkins writes need `build.*`; GitHub fix PRs accept any domain — [agent/policy.py:64-81](C:/Dev/Helix/agent/policy.py#L64-L81), [docs/architecture/safety-policy.md:26-44](C:/Dev/Helix/docs/architecture/safety-policy.md#L26-L44). The doc records the motivating defect: a failing unit test once let arm E restart and roll back the live sandbox unapproved — [docs/architecture/safety-policy.md:38-42](C:/Dev/Helix/docs/architecture/safety-policy.md#L38-L42).
- **Two enforcement layers plus transport bounds** are documented explicitly: coordinator routing, `enforce_execution_gate()` before any transport call, and client-level refusals for kube contexts, branches and paths — [docs/architecture/safety-policy.md:52-78](C:/Dev/Helix/docs/architecture/safety-policy.md#L52-L78). The executor gate recomputes the verdict and loads the approval row for the candidate fingerprint — [agent/execution/__init__.py:156-169](C:/Dev/Helix/agent/execution/__init__.py#L156-L169).
- **Approval is durable:** the pending `approvals` row and the `validating -> awaiting_approval` transition are written in one transaction; the row carries `(action, params, fingerprint)` — [docs/architecture/safety-policy.md:80-86](C:/Dev/Helix/docs/architecture/safety-policy.md#L80-L86).
- **Retry bounds:** `max_attempts = 3` and `approval_timeout = 86400.0` by default — [helix/config.py:162-172](C:/Dev/Helix/helix/config.py#L162-L172); step timeout defaults to 600 s — [.env.example:122](C:/Dev/Helix/.env.example#L122).
- **Compensation exists:** the catalogue declares a compensating action for rollback, scale and reapply; failed verification with unpaid compensation debt routes to a compensate node before ending — [agent/actions/catalogue.py:299-352](C:/Dev/Helix/agent/actions/catalogue.py#L299-L352), [agent/agents/coordinator.py:1033-1046](C:/Dev/Helix/agent/agents/coordinator.py#L1033-L1046).
- **Verification is a real post-action stage**, not an assumption: `executing -> verifying -> recovered` on pass, with failed verification routing back to replanning or escalation — [docs/architecture/state-machine.md:45-56](C:/Dev/Helix/docs/architecture/state-machine.md#L45-L56).
- **Code-level GitHub write bounds:** `_check_branch` permits only `helix/INC-*`; `_check_path` permits only `sandbox/payment-api/*` with no `..`; both fire before any HTTP request — [integrations/github/write.py:1-19](C:/Dev/Helix/integrations/github/write.py#L1-L19), [integrations/github/write.py:47-64](C:/Dev/Helix/integrations/github/write.py#L47-L64).
- **No merge:** the module docstring says "there is deliberately no merge call anywhere in this module: Helix proposes, humans merge"; `open_fix_pr` creates a branch, commits files and opens a PR, and `close_pr` closes it as compensation — [integrations/github/write.py:1-7](C:/Dev/Helix/integrations/github/write.py#L1-L7), [integrations/github/write.py:148-211](C:/Dev/Helix/integrations/github/write.py#L148-L211), [integrations/github/write.py:213-257](C:/Dev/Helix/integrations/github/write.py#L213-L257).

### Inferences
- This is a **capability-constrained** design rather than a prompt-constrained one: the model's freedom is bounded by the action schema, and the safety decision is a pure function that can be unit-tested. That is materially stronger than "the agent is told not to do dangerous things."
- The weakest link is not arbitrary shell access but **action selection**: a wrong but allowed low-risk action (e.g. restarting the wrong deployment in a registered namespace) can still execute without approval. The benchmark's run-1 unsafe restarts on `oom-killed` are an example of exactly this class — [docs/benchmark/phase-2-evaluation.md:211-215](C:/Dev/Helix/docs/benchmark/phase-2-evaluation.md#L211-L215).
- The code-level path/branch bounds are the strongest part of the model because they hold even if policy and routing are both wrong; they are also narrow to one sandbox service, so they do not generalise to a customer's repositories.
- There is no global kill switch, pause-all, or per-service disable in the code I searched; stopping Helix means stopping the process or removing credentials. That is a notable gap against the safety playbook in the sibling evidence note.

### Gaps
- No evidence of a **global kill switch**, "pause all actions", or an emergency stop endpoint; a search for kill-switch/pause/emergency terms found only an unrelated CLI comment.
- No evidence that policy verdicts or execution refusals are exposed as metrics/alerts; they are rows and logs, not an operational SLO surface.
- The `ALLOW` verdict is currently unused because every catalogue write is at least low-tier — [docs/architecture/safety-policy.md:21](C:/Dev/Helix/docs/architecture/safety-policy.md#L21).

---

## 4. What is the architecture, state model and data layer underneath?

### Takeaway
A LangGraph `StateGraph` over a shared `WorkflowState`, with a single coordinator that owns every status transition and worker nodes that only write outputs. Postgres holds incidents, evidence, hypotheses, candidates, validations, approvals, executions, verifications, events, triage/review rows, work items, outbox and team/service tables. Kafka carries external events; a transactional outbox projects state to Slack/Jira/Notion; Neo4j is an optional advisory graph.

### Cited Findings
- The workflow is "a LangGraph `StateGraph` over `agent.state.WorkflowState`" — [docs/architecture/agent-graph.md:3](C:/Dev/Helix/docs/architecture/agent-graph.md#L3); `build_graph` registers intake, coordinator, triage/review and the recovery nodes, with conditional edges from the coordinator — [agent/graph.py:652-726](C:/Dev/Helix/agent/graph.py#L652-L726).
- "Coordinator: the only place incident status transitions happen"; worker nodes never touch status — [agent/agents/coordinator.py:1-13](C:/Dev/Helix/agent/agents/coordinator.py#L1-L13).
- Every transition is validated against `ALLOWED_TRANSITIONS` and writes a `transitions` row plus updates `incidents.status` — [state/machine.py:77-77](C:/Dev/Helix/state/machine.py#L77), [state/machine.py:193-224](C:/Dev/Helix/state/machine.py#L193-L224), [docs/architecture/state-machine.md:60-63](C:/Dev/Helix/docs/architecture/state-machine.md#L60-L63).
- **17 statuses** are defined: 10 recovery statuses and 7 change statuses — [state/machine.py:38-62](C:/Dev/Helix/state/machine.py#L38-L62), [docs/architecture/state-machine.md:12-30](C:/Dev/Helix/docs/architecture/state-machine.md#L12-L30).
- **24 tables** are declared in `state/models.py` (counted by `__tablename__`), including `incidents`, `signals`, `transitions`, `evidence_items`, `hypotheses`, `candidates`, `validations`, `approvals`, `executions`, `verifications`, `event_records`, `correlation_tokens`, `triage_results`, `review_requests`, `work_items`, `knowledge_pages`, `integration_outbox`, `connections`, `pr_bot_settings`, `people`, `invitations`, `services` — [state/models.py:131-1197](C:/Dev/Helix/state/models.py#L131-L1197).
- **20 Alembic migrations**, `0001` through `0020`, live under `state/migrations/versions/`; migrations run at every boot — [HANDOFF-NIKHIL.md:221-226](C:/Dev/Helix/HANDOFF-NIKHIL.md#L221-L226).
- **Event taxonomy:** 26 `EventType` values across source control, CI, deployment/runtime, Helix lifecycle and Phase 2 change lifecycle — [events/schemas.py:102-142](C:/Dev/Helix/events/schemas.py#L102-L142).
- **Ingest is idempotent and correlating:** a repeated `event_id` is returned as a duplicate with nothing written; otherwise the event is addressed by `correlation_id`, then by shared tokens, or opens a new incident — [events/ingest.py:249-291](C:/Dev/Helix/events/ingest.py#L249-L291).
- **Outbox guarantees:** rows commit in the same transaction as the transition; a row reaches `sent` only after the provider call; creates are idempotent via the local mirror and provider lookup; 10 attempts before `failed`; `FOR UPDATE SKIP LOCKED` lets concurrent drains coexist — [integrations/outbox.py:8-26](C:/Dev/Helix/integrations/outbox.py#L8-L26), [integrations/outbox.py:484-497](C:/Dev/Helix/integrations/outbox.py#L484-L497), [integrations/outbox.py:500-513](C:/Dev/Helix/integrations/outbox.py#L500-L513).
- **API surface:** 15 HTTP routes in `helix/api.py` (intake, incidents, approvals, reviews, Slack interactions, GitHub webhook) plus 44 router routes across auth/UI/team/services/connections, for 59 route decorators total (counted by decorator pattern). The review/approval endpoints are `/approvals/{id}/approve|reject`, `/reviews/{id}/approve|reject`, `/reviews/{id}/reassign` — [helix/api.py:453-490](C:/Dev/Helix/helix/api.py#L453-L490), [helix/api.py:998-1073](C:/Dev/Helix/helix/api.py#L998-L1073).
- **CLI surface:** 20 top-level commands plus nested `team import` (21 `add_parser` calls), including `serve`, `intake`, `run`, `consume`, `watch`, `slack-socket`, `sync`, `approve`, `reject`, `reviews`, `review`, `export-history`, `team import` — [helix/cli.py:36-273](C:/Dev/Helix/helix/cli.py#L36-L273).
- **Neo4j is optional and advisory:** `GraphStore` has memory and Neo4j implementations; `HELIX_GRAPH_ENABLED` defaults to false; an unreachable graph falls back to the registry — [graph/store.py:1-19](C:/Dev/Helix/graph/store.py#L1-L19), [helix/config.py:199](C:/Dev/Helix/helix/config.py#L199), [agent/agents/review.py:41-64](C:/Dev/Helix/agent/agents/review.py#L41-L64).
- **Postgres is authoritative, not Kafka:** lifecycle events are written to `event_records` in the same transaction as the transition, then published separately — [plan.md:181](C:/Dev/Helix/plan.md#L181).

### Inferences
- The architecture is deliberately "boring control plane, typed edges": Postgres + outbox + a single transition owner. That is what makes the audit trail and resume semantics credible.
- The event taxonomy is broader than the wired surface: `review_events` and `PR_MERGED` exist but are not wired, so the implemented change loop observes PR open/update and check runs, not GitHub review or merge state.
- The Neo4j projection is not on the critical path in the hosted product (`HELIX_GRAPH_ENABLED=false`), so graph-backed reviewer selection is a benchmark/self-host feature, not a shipped one — [render.yaml:78-79](C:/Dev/Helix/render.yaml#L78-L79).

### Gaps
- No load, soak, or concurrency test results were found for the API or outbox at multi-user scale.
- The repository does not document a backup/restore procedure for the hosted Postgres beyond the migration/expiry notes.
- I did not verify that all 20 migrations apply cleanly from zero in this environment.

---

## 5. Which integrations exist, with what auth model and read/write capability?

### Takeaway
Helix has real integrations for Slack, Jira/Atlassian, GitHub (App + PAT), Kubernetes, Jenkins, Docker, Elasticsearch/ELK, Notion, Linear/Trello and email via Resend, plus fixture/memory fakes. Read paths are broad; write paths are narrow and bounded. Tokens for connected accounts are Fernet-encrypted at rest; inbound Slack and GitHub requests are signature-verified.

### Cited Findings
- **Slack:** OAuth install and token storage, Socket Mode for local button clicks, and HMAC signature verification with a 300-second replay window — [helix/connections_ui.py:5-28](C:/Dev/Helix/helix/connections_ui.py#L5-L28), [integrations/slack/signature.py:26-55](C:/Dev/Helix/integrations/slack/signature.py#L26-L55), [README.md:97](C:/Dev/Helix/README.md#L97). Writes: post messages/cards and update them; reads: `auth.test`, channel listing.
- **Jira:** Atlassian OAuth 2.0 3LO plus basic-auth fallback; capabilities include create/get/find/update/assign/transition/comment/link work items — [helix/connections_ui.py:8-10](C:/Dev/Helix/helix/connections_ui.py#L8-L10), [integrations/work/jira.py:155-178](C:/Dev/Helix/integrations/work/jira.py#L155-L178), [integrations/work/contract.py:112-158](C:/Dev/Helix/integrations/work/contract.py#L112-L158).
- **GitHub:** a GitHub App (JWT → installation token) with PAT fallback; read capabilities for commit, diff, PR info, PR reviews, check runs and branch state — [integrations/github/client.py:90-222](C:/Dev/Helix/integrations/github/client.py#L90-L222), [integrations/github/app.py](C:/Dev/Helix/integrations/github/app.py). Writes: create `helix/INC-*` branch, commit files under `sandbox/payment-api/`, open a PR, close a PR — [integrations/github/write.py:148-257](C:/Dev/Helix/integrations/github/write.py#L148-L257).
- **Kubernetes:** read evidence (pods, events, logs, deployment state, secrets metadata) and write exactly four operations: rollout restart, rollout undo, scale, apply an allowlisted manifest — [agent/execution/kubernetes.py:37-58](C:/Dev/Helix/agent/execution/kubernetes.py#L37-L58), [agent/actions/catalogue.py:282-352](C:/Dev/Helix/agent/actions/catalogue.py#L282-L352). Writes are constrained to allowed kube contexts — [docs/architecture/safety-policy.md:72-78](C:/Dev/Helix/docs/architecture/safety-policy.md#L72-L78).
- **Jenkins:** read build status/job info, and `rerun_pipeline` triggers a build — [integrations/jenkins/client.py:9-14](C:/Dev/Helix/integrations/jenkins/client.py#L9-L14), [agent/actions/catalogue.py:381-392](C:/Dev/Helix/agent/actions/catalogue.py#L381-L392).
- **Docker:** read-only host/daemon inspection (`docker ps`, image and container state); no write path — [integrations/docker/client.py:1-4](C:/Dev/Helix/integrations/docker/client.py#L1-L4), [integrations/docker/client.py:79-145](C:/Dev/Helix/integrations/docker/client.py#L79-L145).
- **ELK/Elasticsearch:** log search as evidence — [integrations/capabilities.py:112](C:/Dev/Helix/integrations/capabilities.py#L112), [integrations/elk](C:/Dev/Helix/integrations/elk).
- **Notion:** create page and append blocks; a `KnowledgePage` row prevents duplicate pages — [integrations/notion/client.py:23-41](C:/Dev/Helix/integrations/notion/client.py#L23-L41), [integrations/outbox.py:444-475](C:/Dev/Helix/integrations/outbox.py#L444-L475).
- **Linear/Trello:** optional work-management providers behind the same `WorkManagementProvider` protocol; capability gaps (e.g. Trello has no native priority/state) are declared rather than hidden — [integrations/work/contract.py:1-31](C:/Dev/Helix/integrations/work/contract.py#L1-L31), [plan.md:191](C:/Dev/Helix/plan.md#L191).
- **Email:** invites via Resend's HTTPS API because Render blocks SMTP — [render.yaml:47-54](C:/Dev/Helix/render.yaml#L47-L54), [HANDOFF-NIKHIL.md:248-259](C:/Dev/Helix/HANDOFF-NIKHIL.md#L248-L259).
- **Fixtures:** `HELIX_EVIDENCE_SOURCE=fixtures` selects recorded evidence clients, and the hosted app runs this way — [render.yaml:68-73](C:/Dev/Helix/render.yaml#L68-L73), [integrations/capabilities.py:181-239](C:/Dev/Helix/integrations/capabilities.py#L181-L239).
- **Token encryption:** connected-account access/refresh tokens are Fernet-encrypted before storage under a key derived from `HELIX_CONNECTIONS_KEY`; an undecryptable token reads as "not connected" — [state/connections.py:1-7](C:/Dev/Helix/state/connections.py#L1-L7), [state/connections.py:45-99](C:/Dev/Helix/state/connections.py#L45-L99).
- **Inbound auth:** GitHub webhook verifies `X-Hub-Signature-256` with `hmac.compare_digest` and returns 404 if no secret is configured — [helix/api.py:621-641](C:/Dev/Helix/helix/api.py#L621-L641); Slack verifies its signature before handling — [helix/api.py:581-601](C:/Dev/Helix/helix/api.py#L581-L601); intake uses a bearer token or a signed-in session — [helix/api.py:108-127](C:/Dev/Helix/helix/api.py#L108-L127).

### Inferences
- The integration set is broad but **one-account-per-provider** for the whole deployment: connections are singular, so a second team connecting Slack/Jira would replace the first — [HANDOFF-NIKHIL.md:374-375](C:/Dev/Helix/HANDOFF-NIKHIL.md#L374-L375).
- The write surface is intentionally asymmetric: broad read evidence, narrow write actions. This is the opposite of a general automation platform and is the core of Helix's safety story.
- Jira/Linear/Trello are used as **work-item mirrors**, not as review systems. Helix comments on them at review transitions — [integrations/lifecycle_sync.py:158-173](C:/Dev/Helix/integrations/lifecycle_sync.py#L158-L173).

### Gaps
- I did not verify any live integration against a real Slack workspace, Jira site, GitHub App installation, Kubernetes cluster, Jenkins server, or Notion workspace. The repository itself flags the Slack check as simulated (#44) and the GitHub App as failing with `http_401` on Render (#52) — [HANDOFF-NIKHIL.md:364-368](C:/Dev/Helix/HANDOFF-NIKHIL.md#L364-L368).
- No rate-limit or retry policy comparison across providers was performed; only the outbox's bounded retry ledger was verified.

---

## 6. What evidence exists that Helix works — tests, benchmark, and the OPT-1 model?

### Takeaway
The repository contains a substantial test suite and a five-arm, 19-scenario benchmark. The strongest measured claim is **tolerance PASS over three final runs of the 13 Postgres-only scenarios (65/65 outcomes per run), with zero unsafe executions in the final runs**. The benchmark is small, partly single-run (six cluster scenarios), and arm A is a deterministic control written against the same catalogue, so it bounds the LLM arms rather than demonstrating general capability. OPT-1's model is explicitly "no better than baseline."

### Cited Findings
- **Benchmark shape:** 19 YAML scenarios under `benchmark/scenarios/`; five arms A–E (A rules, B single LLM call, C plus retrieval, D plus graph context, E full Helix) — [README.md:139-144](C:/Dev/Helix/README.md#L139-L144), [docs/benchmark/phase-2-evaluation.md:11-19](C:/Dev/Helix/docs/benchmark/phase-2-evaluation.md#L11-L19).
- **Final stability runs:** "Three fresh runs of the 13 Postgres-only scenarios, arms A to E … 65 of 65 outcomes per run, none skipped"; the tolerance command reports **PASS** — [docs/benchmark/phase-2-evaluation.md:36-45](C:/Dev/Helix/docs/benchmark/phase-2-evaluation.md#L36-L45).
- **Recovery results (six scenarios, runs 1/2/3):** arm A root cause 6/6/6, valid action 6/6/6, recovered 6/6/6, unsafe 0; arm B recovered 0/0/0; arm C recovered 0/0/0; arm D recovered 0/0/0; arm E recovered 1/0/4 — [docs/benchmark/phase-2-evaluation.md:47-55](C:/Dev/Helix/docs/benchmark/phase-2-evaluation.md#L47-L55).
- **Change results (seven scenarios, every run):** owner 6/6 and reviewer 4/4 on every arm, zero duplicate work items, lifecycle 7/7 for A, 5/7 for B/C/D (structural), 6/7 for E — [docs/benchmark/phase-2-evaluation.md:57-60](C:/Dev/Helix/docs/benchmark/phase-2-evaluation.md#L57-L60).
- **Zero unsafe executions in the final runs**, and run 1's two unsafe restarts on `oom-killed` are disclosed as a finding — [docs/benchmark/phase-2-evaluation.md:211-215](C:/Dev/Helix/docs/benchmark/phase-2-evaluation.md#L211-L215).
- **Scope limits are stated:** the five-arm ablation populated all 19 scenarios once, and the 13 Postgres-only scenarios five times; stability of the six cluster scenarios is explicitly *not* established because they ran once — [docs/benchmark/phase-2-evaluation.md:240-247](C:/Dev/Helix/docs/benchmark/phase-2-evaluation.md#L240-L247).
- **Arm E run 3 was rerun** because both Groq keys hit daily token caps; the rerun replaces the void outcomes and tolerance still passes — [docs/benchmark/phase-2-evaluation.md:62-73](C:/Dev/Helix/docs/benchmark/phase-2-evaluation.md#L62-L73).
- **Over-asking is counted as a finding, not a failure:** in six places LLM arms proposed a riskier action than needed, policy stopped it, and it was rejected; "Nothing unsafe ran." — [docs/benchmark/phase-2-evaluation.md:75-82](C:/Dev/Helix/docs/benchmark/phase-2-evaluation.md#L75-L82).
- **Arm A dominates every recovery metric** ("12 of 12"), and arm E is the only LLM arm that recovers anything on its own — [docs/benchmark/phase-2-evaluation.md:158-172](C:/Dev/Helix/docs/benchmark/phase-2-evaluation.md#L158-L172).
- **Arm A is deterministic tables** (`agent/rules.py`) written for the closed catalogue — [docs/benchmark/methodology.md:21](C:/Dev/Helix/docs/benchmark/methodology.md#L21); the rules map signatures to ordered actions — [agent/rules.py:99-113](C:/Dev/Helix/agent/rules.py#L99-L113).
- **OPT-1:** a separate Python 3.11 uv project using PyCaret 3.3.2; 305 actions exported, 140 from arms B/C/D excluded, 125 train / 40 held out; held-out accuracy **0.900 vs 0.850 baseline**, verdict **"no better than baseline"** — [mlops/README.md:3-8](C:/Dev/Helix/mlops/README.md#L3-L8), [mlops/README.md:37-49](C:/Dev/Helix/mlops/README.md#L37-L49).
- **Static test inventory:** 1,992 `def test_` occurrences in the 135 Python files under `tests/` (125 are `test_*.py`; 126 contain test functions), plus 16 in `mlops/tests/` and `sandbox/payment-api/tests/`; 18 `pytest.mark.integration` markers; `addopts = "-m 'not integration'"` excludes them from a bare `pytest` — [pyproject.toml:46-55](C:/Dev/Helix/pyproject.toml#L46-L55).
- **Documented test counts conflict:** `HANDOFF-NIKHIL.md` says "2462 unit pass, 51 integration deselected" — [HANDOFF-NIKHIL.md:6](C:/Dev/Helix/HANDOFF-NIKHIL.md#L6); `docs/verifying.md` says "1408 unit tests" and expects "1408 passed, 50 deselected" — [docs/verifying.md:14-20](C:/Dev/Helix/docs/verifying.md#L14-L20). The `.pytest_cache` nodeids file contains 1,328 IDs and `lastfailed` 16 IDs (local cache, not authoritative).
- **Local collection attempt:** `python -m pytest --collect-only -q` in this checkout collected 1,080 of 1,113 tests (33 deselected) with **31 collection errors**, all caused by `ModuleNotFoundError: No module named 'cryptography'`; `cryptography` is a declared dependency — [pyproject.toml:26](C:/Dev/Helix/pyproject.toml#L26). This is local environment drift, not evidence that CI fails.
- **CI:** one GitHub Actions workflow runs `uv sync`, `ruff check`, `ruff format --check`, and `pytest`; there is no type checker, coverage gate, SAST, dependency audit or Sonar step — [.github/workflows/ci.yml:7-28](C:/Dev/Helix/.github/workflows/ci.yml#L7-L28).
- **SonarQube is not part of Helix's own review path:** it appears only in the sandbox Jenkins pipeline (`stage('SonarQube analysis')`, quality gate) and its Compose/Ansible setup — [infra/jenkins/Jenkinsfile:55-130](C:/Dev/Helix/infra/jenkins/Jenkinsfile#L55-L130), [infra/sonarqube-compose.yml](C:/Dev/Helix/infra/sonarqube-compose.yml). A search of `agent/`, `events/`, `integrations/`, `helix/` and `state/` found no Sonar reference.

### Inferences
- The benchmark supports a **narrow, honest claim**: on 13 scripted Postgres-only scenarios, the pipeline is stable across three runs and never executed an unsafe action in the final runs. It does not support a general "Helix resolves incidents" claim, and the document says so.
- Arm A's perfect recovery score is best read as **catalogue fit**, not model capability: the rules were written for the same closed set of faults and actions. The meaningful comparison is therefore B–E against A, not A against reality.
- The LLM arms' recovery numbers (E: 1/0/4 of 6) show high variance and weak autonomous recovery; Helix's practical value in the benchmark is the **safety and routing layer**, not LLM remediation quality.
- The test-count conflict is a documentation-quality signal. The static count (1,992 functions) and the collected count (1,113 in a partial environment) cannot both correspond to "2,462 unit tests" without heavy parametrization; I could not resolve which number is current.

### Gaps
- **Could not run the suite**: 31 collection errors from a missing `cryptography` package in `.venv`. The documented 2,462 / 51 and 1,408 / 50 counts are therefore unverified.
- No coverage percentage, mutation score, flaky-test rate, or CI runtime was found in the repository.
- The six cluster scenarios have one run each; no stability claim is possible for them.
- No independent adjudication of benchmark correctness exists; the harness scores its own runs.

---

## 7. What is the shipped product surface, and how operationally mature is it?

### Takeaway
The shipped product is a single-team, sign-in-gated web app on Render's free tier: a landing page, dashboard, incident/approval/review queues, connections, team, services and invites, plus a GitHub webhook and Slack interactions. It runs recorded evidence and the rules arm only. It is explicitly pre-multi-tenant, has no RLS, cannot revoke sessions, depends on a free Postgres that expires 2026-11-13, and has several open integration defects.

### Cited Findings
- **Hosting:** Render Blueprint, Docker runtime, `plan: free`, `autoDeploy: false`, health check `/healthz` — [render.yaml:10-17](C:/Dev/Helix/render.yaml#L10-L17). The database is also `plan: free` — [render.yaml:83-87](C:/Dev/Helix/render.yaml#L83-L87).
- **Hosted runtime is fixtures + arm A:** `HELIX_EVIDENCE_SOURCE=fixtures`, `HELIX_FIXTURE_PATH=ci/docker-build-failure`, `HELIX_ARM=A`, `HELIX_WORK_PROVIDER=memory`, `HELIX_GRAPH_ENABLED=false`, `HELIX_VERIFY_FUNCTIONAL=false` — [render.yaml:66-81](C:/Dev/Helix/render.yaml#L66-L81).
- **Auth:** GitHub OAuth sign-in, HMAC-SHA256-signed cookie holding login + expiry, 12-hour session, no passwords, OAuth `state` as a second signed cookie — [helix/auth.py:18-20](C:/Dev/Helix/helix/auth.py#L18-L20), [helix/auth.py:45-47](C:/Dev/Helix/helix/auth.py#L45-L47). Allow-list is `HELIX_ALLOWED_USERS` if set, else the `people` table — [helix/auth.py:76-84](C:/Dev/Helix/helix/auth.py#L76-L84).
- **Pages:** `/` landing, `/ui/` home, `/ui/incidents`, `/ui/incidents/{id}`, `/ui/approvals`, `/ui/connections`, `/ui/team`, `/ui/services`, `/invite/{token}`, `/ui/team/link-slack`, `/healthz`, `/webhooks/github`, `POST /reviews/{id}/reassign` — [HANDOFF-NIKHIL.md:189-202](C:/Dev/Helix/HANDOFF-NIKHIL.md#L189-L202).
- **Decisions feel instant:** the dashboard sends `?wait=false`, gets a 202, and resumes in the background — [HANDOFF-NIKHIL.md:176-181](C:/Dev/Helix/HANDOFF-NIKHIL.md#L176-L181), [helix/api.py:998-1035](C:/Dev/Helix/helix/api.py#L998-L1035).
- **Invites:** 7-day expiry; invite emails via Resend; without SPF/DKIM/DMARC they land in spam — [state/team.py:33](C:/Dev/Helix/state/team.py#L33), [HANDOFF-NIKHIL.md:248-259](C:/Dev/Helix/HANDOFF-NIKHIL.md#L248-L259), [HANDOFF-NIKHIL.md:345](C:/Dev/Helix/HANDOFF-NIKHIL.md#L345).
- **Free-tier limits:** service sleeps after 15 idle minutes; UptimeRobot pings every 5 minutes; 750 instance-hours/month; **free Postgres expires 2026-11-13** — [HANDOFF-NIKHIL.md:260-265](C:/Dev/Helix/HANDOFF-NIKHIL.md#L260-L265).
- **Open product gaps (self-reported):**
  - GitHub App credentials refused with `http_401`, so the PR bot cannot open real PRs on Render (#52) — [HANDOFF-NIKHIL.md:364-366](C:/Dev/Helix/HANDOFF-NIKHIL.md#L364-L366).
  - Slack people-matching tested only against simulated replies (#44) — [HANDOFF-NIKHIL.md:367-368](C:/Dev/Helix/HANDOFF-NIKHIL.md#L367-L368).
  - No row-level security; any signed-in person reads all data; no table has a team column (#58) — [HANDOFF-NIKHIL.md:369-371](C:/Dev/Helix/HANDOFF-NIKHIL.md#L369-L371).
  - Sessions cannot be revoked; a copied cookie works for up to 12 hours (#60) — [HANDOFF-NIKHIL.md:372-373](C:/Dev/Helix/HANDOFF-NIKHIL.md#L372-L373).
  - Single team only; a second team's connections would replace the first (#38) — [HANDOFF-NIKHIL.md:374-375](C:/Dev/Helix/HANDOFF-NIKHIL.md#L374-L375).
  - Old demo incidents remain (#42) — [HANDOFF-NIKHIL.md:350](C:/Dev/Helix/HANDOFF-NIKHIL.md#L350).
- **Multi-tenancy is planned, not built:** Stage 3 requires an organisation model on every row, per-org workers/limits and scoped Kafka topics — [future_plans.md:88-95](C:/Dev/Helix/future_plans.md#L88-L95).
- **Open product questions remain:** hosted vs self-hosted, whether the sandbox cluster is part of the product, and which tools are first-class — [future_plans.md:279-287](C:/Dev/Helix/future_plans.md#L279-L287).

### Inferences
- Operationally, this is a **credible demo/product hybrid for one team**, not a multi-tenant SaaS. The free-tier sleep, DB expiry and manual deploys make it unsuitable for an availability commitment.
- The most consequential near-term risk is the **database expiry**: every incident, team member, service and connection is lost if the move to Neon (#48) does not happen — [HANDOFF-NIKHIL.md:37-39](C:/Dev/Helix/HANDOFF-NIKHIL.md#L37-L39).
- The hosted app cannot demonstrate the most interesting capabilities (live evidence, LLM diagnosis, real fix PRs, cluster actions). A buyer evaluating the URL alone would see a review/approval workflow on recorded data.

### Gaps
- No uptime history, error budget, incident log for Helix itself, or performance measurement was found.
- No backup/restore test or disaster-recovery runbook for the hosted database was found.
- No evidence of a completed live Slack or GitHub App installation on the hosted deployment.

---

## 8. Where does Helix overlap with CodeRabbit?

### Takeaway
The overlap is **at the PR trigger and the change-context layer**, not at the code-critique layer. Both are invoked when a PR opens, both derive changed files/ownership/blast-radius context, and both integrate GitHub and Slack. CodeRabbit's core output is automated review findings on the diff; Helix's core output is a **human reviewer assignment plus a durable approve/reject gate**, followed by bounded remediation. They are complementary far more than competitive, but Helix currently has no way to consume CodeRabbit's findings as review input.

### Cited Findings
- **Same trigger:** Helix opens a change incident on `pr.opened` / `pr.updated` — [events/adapters/github.py:113-118](C:/Dev/Helix/events/adapters/github.py#L113-L118). CodeRabbit is positioned as an agentic reviewer that "challenges every change before the merge" and reviews pull requests — [landscape_evidence.md:20](C:/Dev/Helix/research_notes/Helix%20vs%20CodeRabbit%20comparison/landscape_evidence.md#L20), [landscape_evidence.md:78-80](C:/Dev/Helix/research_notes/Helix%20vs%20CodeRabbit%20comparison/landscape_evidence.md#L78-L80).
- **Same context inputs, different use:** Helix uses changed file paths for blast radius and CODEOWNERS-style ownership — [agent/agents/triage.py:32-40](C:/Dev/Helix/agent/agents/triage.py#L32-L40), [agent/ops/registry.py:172-193](C:/Dev/Helix/agent/ops/registry.py#L172-L193). CodeRabbit sells "Triage" and "Blast Radius Audit" as product capabilities, with Triage starting at the Team tier and Blast Radius at Advanced — [landscape_evidence.md:78-79](C:/Dev/Helix/research_notes/Helix%20vs%20CodeRabbit%20comparison/landscape_evidence.md#L78-L79).
- **Same collaboration surfaces:** Helix posts review requests and approve/request-changes buttons to Slack — [integrations/slack/format.py:235-258](C:/Dev/Helix/integrations/slack/format.py#L235-L258); CodeRabbit's pricing/feature set includes PR review and IDE/CLI surfaces in the reviewed material — [landscape_evidence.md:78-80](C:/Dev/Helix/research_notes/Helix%20vs%20CodeRabbit%20comparison/landscape_evidence.md#L78-L80).
- **Different artifact:** Helix's review decision is a row with `reviewer`, `status`, `decided_by`, `decided_at`, `note` — [state/models.py:881-921](C:/Dev/Helix/state/models.py#L881-L921). It contains no file, line, hunk, severity, category or suggestion fields. CodeRabbit's documented output is review comments/findings, with independent benchmarks scoring comment precision/recall — [landscape_evidence.md:69-74](C:/Dev/Helix/research_notes/Helix%20vs%20CodeRabbit%20comparison/landscape_evidence.md#L69-L74).
- **Helix reads diffs, but not to review code:** `GitHubEvidence.get_diff` returns per-file `filename`, `status`, `additions`, `deletions` and `patch` — [integrations/github/client.py:112-132](C:/Dev/Helix/integrations/github/client.py#L112-L132). In the change path, only the *paths* are used (`ctx.get("files")`) — [agent/agents/triage.py:145-174](C:/Dev/Helix/agent/agents/triage.py#L145-L174). The prompt package contains diagnosis, fused and planning prompts only — no code-review prompt — [agent/prompts](C:/Dev/Helix/agent/prompts).
- **No review comments are posted:** a repository-wide search found no `pulls/{n}/comments`, no review-comment creation, and no POST to `pulls/{n}/reviews`; the only GitHub writes are branch creation, file contents, PR open and PR close — [integrations/github/write.py:148-257](C:/Dev/Helix/integrations/github/write.py#L148-L257).
- **No merge:** "Helix opens fix PRs but never merges them" — [README.md:65-66](C:/Dev/Helix/README.md#L65-L66); the GitHub write module repeats "there is deliberately no merge call anywhere in this module" — [integrations/github/write.py:1-7](C:/Dev/Helix/integrations/github/write.py#L1-L7).
- **CodeRabbit can be observed only as generic check evidence:** Helix's webhook records `check_run` conclusions as `build.passed`/`build.failed` — [events/adapters/github.py:19-26](C:/Dev/Helix/events/adapters/github.py#L19-L26), [helix/api.py:683-704](C:/Dev/Helix/helix/api.py#L683-L704). On the hosted app check runs are "only recorded", never run — [helix/api.py:625-631](C:/Dev/Helix/helix/api.py#L625-L631). There is no parser for review bodies or comment payloads.

### Inferences
- **The products answer different questions.** CodeRabbit: "what is wrong with this diff?" Helix: "who must approve this change, did they, and what bounded action should happen next?" A team could use both without functional conflict.
- **The closest overlap is CodeRabbit Triage/Blast Radius**, because Helix computes a deterministic blast radius and routes owners. Helix's version is narrower (three levels from path heuristics, no dependency graph in the change path) but is included in the product rather than gated to a top tier.
- **The most useful integration would be one-way:** ingest CodeRabbit findings as evidence on the change incident, then require the human reviewer to acknowledge or waive them. Helix currently has no data model for findings, severities or waivers, so this would be new schema, not configuration.
- **A CodeRabbit check failing today does not block Helix's review.** It becomes a `build.failed` event; on the hosted app it is recorded but not diagnosed. Nothing in the review gate reads check conclusions.

### Gaps
- The sibling evidence note does not establish whether CodeRabbit assigns human reviewers, so I cannot claim overlap or non-overlap on that specific function.
- No CodeRabbit API/webhook contract was inspected in this repository, so the feasibility of ingesting its findings is an inference, not a verified integration path.
- No pricing comparison is made here; the sibling note records CodeRabbit's tiers but Helix has no price.

---

## 9. What does Helix not do?

### Takeaway
Helix does **not** perform automated code review. It does not analyse diff semantics for defects, post inline comments, suggest code changes on a PR, approve or request changes on GitHub, merge, run SAST/SCA/secrets scanning, enforce coverage or quality gates, or manage branch protection. It also does not multi-tenant, self-serve sign-up, revoke sessions, or deploy code itself. Its "review" is a human routing and approval workflow; its "fix" is a bounded remediation PR for an incident, not a response to review feedback.

### Cited Findings
- **No line-level review:** the `review_requests` table has reviewer, rationale, status, decided_by/at and note only — [state/models.py:881-921](C:/Dev/Helix/state/models.py#L881-L921). There is no finding/comment/line/severity model anywhere in the 24 tables — [state/models.py:131-1197](C:/Dev/Helix/state/models.py#L131-L1197).
- **No review-comment API usage:** the only GitHub write endpoints used are `POST /git/refs`, `PUT /contents/{path}`, `POST /pulls`, `GET /pulls?...`, and `PATCH /pulls/{n}` (close) — [integrations/github/write.py:163-253](C:/Dev/Helix/integrations/github/write.py#L163-L253). No `pulls/{n}/comments` or `pulls/{n}/reviews` write exists.
- **No merge call:** confirmed by the module docstring and by the absence of any merge endpoint in the write client — [integrations/github/write.py:1-7](C:/Dev/Helix/integrations/github/write.py#L1-L7).
- **No automated code critique prompt:** `agent/prompts/` contains only `diagnosis.py`, `fused.py` and `planning.py` — [agent/prompts](C:/Dev/Helix/agent/prompts). The Review agent's job is "who should review this change and why" — [CONTEXT.md:254-258](C:/Dev/Helix/CONTEXT.md#L254-L258).
- **No SAST/SCA/secrets scanning in Helix:** a search for Sonar/Semgrep/CodeQL/Snyk/Bandit in `agent/`, `events/`, `integrations/`, `helix/` and `state/` returned nothing. SonarQube exists only in the sandbox Jenkins pipeline — [infra/jenkins/Jenkinsfile:55-130](C:/Dev/Helix/infra/jenkins/Jenkinsfile#L55-L130).
- **No quality gate in Helix CI:** CI is lint + format + pytest only — [.github/workflows/ci.yml:21-28](C:/Dev/Helix/.github/workflows/ci.yml#L21-L28).
- **No branch-protection or required-check management:** a search found no branch-protection API usage; the only "protection" mention is a read-only `branch_state` evidence field — [integrations/capabilities.py:145](C:/Dev/Helix/integrations/capabilities.py#L145).
- **No deployment execution:** `approved -> deploying` only changes status and waits for external build/deploy events — [agent/agents/coordinator.py:357-366](C:/Dev/Helix/agent/agents/coordinator.py#L357-L366), [agent/agents/coordinator.py:700-711](C:/Dev/Helix/agent/agents/coordinator.py#L700-L711).
- **No arbitrary command execution:** the LLM chooses from six typed actions; there is no shell tool — [README.md:44-47](C:/Dev/Helix/README.md#L44-L47), [agent/actions/catalogue.py:90-95](C:/Dev/Helix/agent/actions/catalogue.py#L90-L95).
- **No multi-tenancy / RLS:** "any signed-in person reads all data, no table has a team column and Postgres RLS is off" — [HANDOFF-NIKHIL.md:369-371](C:/Dev/Helix/HANDOFF-NIKHIL.md#L369-L371); Stage 3 is planned — [future_plans.md:88-95](C:/Dev/Helix/future_plans.md#L88-L95).
- **No self-service signup:** sign-in is allow-listed to the `people` table or `HELIX_ALLOWED_USERS` — [helix/auth.py:76-84](C:/Dev/Helix/helix/auth.py#L76-L84); the product question of when signup opens is still open (#56) — [HANDOFF-NIKHIL.md:342](C:/Dev/Helix/HANDOFF-NIKHIL.md#L342).
- **No session revocation:** a copied cookie works for up to 12 hours — [HANDOFF-NIKHIL.md:372-373](C:/Dev/Helix/HANDOFF-NIKHIL.md#L372-L373).
- **No chat/IDE/CLI review product:** the CLI has `reviews`/`review` commands for the internal gate — [helix/cli.py:231-256](C:/Dev/Helix/helix/cli.py#L231-L256) — but no code-review command, editor integration or PR chat surface.
- **No multi-repo mapping per service beyond one `repo` string:** a delivery is assigned to the first service whose registry `repo` matches; services sharing a repo mean the first by name takes it — [helix/api.py:668-677](C:/Dev/Helix/helix/api.py#L668-L677), [agent/ops/registry.py:54-62](C:/Dev/Helix/agent/ops/registry.py#L54-L62).
- **No automatic response to review feedback:** rejecting a review moves the change to `fix_required` and waits for an updated commit; Helix does not generate a fix for the reviewer's note — [docs/architecture/state-machine.md:52-54](C:/Dev/Helix/docs/architecture/state-machine.md#L52-L54), [agent/agents/coordinator.py:331-338](C:/Dev/Helix/agent/agents/coordinator.py#L331-L338).
- **No evidence of load/security testing of Helix itself:** no load-test, pentest or adversarial-prompt test suite was found; the audit document covers code defects, not adversarial evaluation — [docs/audit-2026-10-05.md:22-66](C:/Dev/Helix/docs/audit-2026-10-05.md#L22-L66).

### Inferences
- The "review" label is the single biggest source of confusion in a CodeRabbit comparison. Helix's review gate is closer to **CODEOWNERS + required approval + audit trail** than to an AI reviewer.
- Helix's genuine differentiator is **post-approval action with policy enforcement and verification**, plus the recovery loop. That is outside CodeRabbit's reviewed scope.
- The absence of a findings model is structural, not a missing checkbox: adding CodeRabbit-style findings would require new tables, new UI, new decision semantics (waive/fix/dismiss) and a new policy gate. It is a roadmap item, not an integration setting.

### Gaps
- I did not inspect every third-party library for transitive review capability; the claim is about Helix's own code and prompts.
- The repository does not state whether Helix will ever post GitHub reviews; the design docs describe GitHub as "the source-of-change and review system" — [CONTEXT.md:276-293](C:/Dev/Helix/CONTEXT.md#L276-L293) — but the implementation keeps the gate internal.

---

## 10. What could not be confirmed, and how confident is each major claim?

### Takeaway
Most architectural and product claims are directly verifiable in code. The weakest claims are **quantitative test results**, **live integration behaviour**, **benchmark generality**, and **any statement that depends on the hosted deployment working end to end**. I could not run the test suite locally, and several documented numbers conflict.

### Cited Findings
- **Test counts conflict:** 2,462 unit + 51 integration in [HANDOFF-NIKHIL.md:6](C:/Dev/Helix/HANDOFF-NIKHIL.md#L6) versus 1,408 unit + 50 deselected in [docs/verifying.md:14-20](C:/Dev/Helix/docs/verifying.md#L14-L20); static count 1,992 test functions under `tests/` (2,008 tracked overall); `.pytest_cache` 1,328 node IDs.
- **Local test run blocked:** 31 collection errors, all `ModuleNotFoundError: No module named 'cryptography'`; 1,080/1,113 collected (33 deselected). `cryptography` is declared at [pyproject.toml:26](C:/Dev/Helix/pyproject.toml#L26), so this is a stale local virtualenv rather than a repository defect.
- **Benchmark scope is self-limited:** 13 Postgres-only scenarios in the final runs; six cluster scenarios ran once — [docs/benchmark/phase-2-evaluation.md:240-247](C:/Dev/Helix/docs/benchmark/phase-2-evaluation.md#L240-L247).
- **Hosted behaviour is fixture-only:** `HELIX_EVIDENCE_SOURCE=fixtures`, `HELIX_ARM=A`, no cluster/Kafka/Jenkins/LLM — [render.yaml:66-81](C:/Dev/Helix/render.yaml#L66-L81), [README.md:103-110](C:/Dev/Helix/README.md#L103-L110).
- **Known live defects are self-reported:** GitHub App `http_401` (#52), Slack matching simulated (#44), invite spam (#64), old demo incidents (#42) — [HANDOFF-NIKHIL.md:340-350](C:/Dev/Helix/HANDOFF-NIKHIL.md#L340-L350), [HANDOFF-NIKHIL.md:359-375](C:/Dev/Helix/HANDOFF-NIKHIL.md#L359-L375).
- **No independent verification:** the benchmark harness scores its own runs; no external audit or replication exists in the repository.
- **No type checking or coverage:** CI runs ruff and pytest only — [.github/workflows/ci.yml:21-28](C:/Dev/Helix/.github/workflows/ci.yml#L21-L28).

### Inferences
- Confidence is **high** for: the review/approval mechanics, the six-action catalogue, policy verdicts and enforcement layers, the state machine, the outbox design, the auth model, and the absence of code-review features — all are directly readable in code.
- Confidence is **medium** for: the product's operational behaviour, because the hosted deployment is fixture-only and several integrations are unverified live.
- Confidence is **low** for: any exact test-pass count, any general incident-resolution rate, and any claim that Helix "works" beyond the 13 scripted scenarios.
- For a CodeRabbit comparison, the defensible statement is: **Helix overlaps on PR-time triage/routing and Slack collaboration; it does not compete on automated code review, and its measured evidence covers incident recovery, not review quality.**

### Gaps
- Exact current unit/integration pass counts could not be established.
- No live end-to-end run of the hosted app, Slack app, GitHub App, Jira, Kubernetes, Jenkins or Notion was performed.
- No independent benchmark, customer reference, or production incident data exists in the repository.
- No measurement of reviewer behaviour (time-to-review, approval quality, rubber-stamping) exists; the benchmark measures owner/reviewer *selection*, not review *effectiveness*.
