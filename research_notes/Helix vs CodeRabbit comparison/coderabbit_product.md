# CodeRabbit: Product Fact Sheet (as of 19 October 2026)

**Purpose.** A self-contained, source-dated reference on CodeRabbit as a product, for a report writer comparing it against an AI incident-response / remediation tool ("Helix"). No further research should be required to use this file.

**Scope.** CodeRabbit's shipped product, packaging, pricing, limits, integrations, security posture, and 2026 change history. Excludes competitor analysis (see `landscape_evidence.md` for independent benchmark evidence, which this file does not duplicate).

**Method.** Direct fetches of primary sources on 17–19 October 2026. Web search was unreliable and was not used as a basis for any fact here.

**Labelling convention.** Every substantive item carries one of:

| Label | Meaning |
| - | - |
| **[D]** Documented | Stated in `docs.coderabbit.ai`, which is the product documentation of record. Treat as how the product is specified to behave. |
| **[V]** Vendor claim | Stated on `coderabbit.ai` marketing pages, blog, or a vendor-authored report. Not independently verified. |
| **[U]** Unverified | Could not be confirmed from a primary source. Do not repeat as fact. |

Where the docs and marketing disagree, the docs version is used and the discrepancy is noted.

### At a glance

| Dimension | CodeRabbit, October 2026 |
| - | - |
| Category | AI code review + pre-merge change management; expanding into security and agentic coding |
| Primary trigger | A push to a PR/MR, or an explicit chat/CLI/IDE request |
| Primary output | Review comments, a PR walkthrough, commits, PRs, triage decisions |
| Actuation surface | Git provider only — comments, commits, PRs, rules-based PR merge/close. **No production actuation** |
| Platforms | GitHub Cloud/GHES, GitLab Cloud/self-managed, Azure DevOps Services, Bitbucket Cloud/DC |
| Plans | Free, OSS, Essentials $24/$30, Team $48/$60, Advanced $72/$90, Enterprise custom (annual/monthly, per dev) |
| Metering | Per-seat + hourly review caps that decay under sustained use + $0.25/reviewed file usage + $0.40/agent minute |
| Languages | "All languages, varying proficiency"; ~25–30 with first-class pattern-based tooling |
| Scale limits | 300 files/review (150 on Essentials/Free), 8 MiB/file for pattern tools, >5 min typical review |
| Compliance | SOC 2 Type II, GDPR documented; HIPAA/ISO 27001 unverified; reports gated |
| 2026 trajectory | Triage, Change Stack, Security, Coding Agent, CLI, Slack/Discord — all PR-scoped |
| Closest overlap with incident response | Change-risk scoring, blast-radius reasoning, "diagnose issues & outages" via Slack (code-level only) |

---

## 1. What CodeRabbit is

**[D]** CodeRabbit is an AI code review product that runs on pull requests and merge requests, plus pre-PR surfaces (CLI, IDE, coding-agent loops). Its core output is review comments and a structured PR walkthrough; its extended surfaces produce fixes, plans, tests, triage decisions, security findings, and — since 2026 — cross-repository change analysis.

**[V]** Positioning language: "the most comprehensive set of tools to help you ship code you trust"; "Challenges every change before the merge, ensuring only the best code lands in your codebase"; "Stop rubberstamping large PRs or delaying them until you have time to understand them."

**[V]** The company frames its 2026 expansion as "Agentic Change Management," announced 12 August 2026 alongside a **$143M Series C at a $1.5B valuation**. The three named pillars are **Triage**, **Change Stack**, and **Security**. (Funding and framing reported by SiliconANGLE, 12 Aug 2026; the product pages themselves are primary for capability.)

**The single most important framing fact for a Helix comparison:** CodeRabbit's actuation surface is **code and pull requests**. It comments, opens PRs, pushes commits, merges/closes PRs (including via enabled Triage rules), and runs post-merge automations (changelog, tickets, notifications). **[D]** No CodeRabbit documentation describes mutating production infrastructure, rolling back a deployment, toggling a feature flag, or executing a runbook against a live system. Its worst-case failure is a bad commit, a wrongly merged PR, or a wrongly closed PR — all confined to the Git provider, all subject to that provider's branch protection and permissions, and all reversible with a revert or reopen. That is a different risk class from an incident-response agent with production write access.

---

## 2. End-to-end workflow

### 2.1 Install and connect

1. **[D]** Install the GitHub app (Cloud or Enterprise Server), GitLab app (Cloud or self-managed), Azure DevOps extension (Services only), or Bitbucket app (Cloud or Data Center). Repositories are selected at install; more can be added later.
2. **[D]** A `.coderabbit.yaml` (or `.coderabbit.yml`) at repo root configures behaviour. Configuration resolves across repository YAML → central/org YAML → UI settings, with an opt-in **configuration inheritance** mode that merges levels instead of taking the highest-priority source.
3. **[D]** Defaults are on-by-default for automatic PR review, with incremental review on every push, and automatic pause after **5** reviewed commits (`auto_pause_after_reviewed_commits: 5`).
4. **[D]** Optional context sources are connected: linked repositories (multi-repo analysis), issue trackers (GitHub/GitLab/Jira/Linear), MCP servers, code guidelines files, and the knowledge base.

### 2.2 On a pull request

1. **[D]** PR opened → CodeRabbit posts a **Walkthrough**: plain-language summary of changed files, sequence diagrams (Mermaid) where useful, estimated review effort on a 1–5 scale, related issues/PRs, an assessment of linked issues, suggested labels and reviewers, and optionally a poem. The walkthrough is a single structured top-of-PR comment.
2. **[D]** Inline review comments are posted as findings, each with a severity and a suggested fix where one can be generated. Findings can be fixed one-click ("Fix comments", formerly "Autofix" — see §9 for the 14 Oct 2026 rename).
3. **[D]** A **Pre-Merge Checks** status is posted. Four built-in checks: docstring coverage (default threshold **80%**), PR title, PR description, and linked-issue assessment. Custom checks (Team+, up to 20) run org-defined instructions with modes `off` / `warning` / `error`.
4. **[D]** The developer can chat with the bot in the PR thread: ask about a change, request a re-review, dispute a finding, or ask for a fix. Chat is rate-limited separately from reviews.
5. **[D]** On each push, an **incremental review** runs against only the new changes (subject to pause/limit rules). A **full review** re-reviews everything.
6. **[D]** Optional **Finishing Touches** run from a checkbox or comment: fix comments, fix CI, resolve merge conflicts, generate unit tests, simplify code, write docstrings, or a custom recipe.
7. **[D]** Merge is gated by whatever the customer's branch protection requires; CodeRabbit's own check can be configured to request changes or block (Security check, §3.5).
8. **[D]** **Post-Merge Actions** (Team+) run follow-up work: changelog entries, ticket updates, notifications.

### 2.3 Outside the PR

- **[D]** **CLI** (`coderabbit` CLI, v0.9.0 as of 8 Oct 2026, beta): review local changes or a remote branch before opening a PR. Remote reviews are rejected at **≥300 changed files**. CLI reviews have their own rate-limit bucket.
- **[D]** **IDE extension** (VS Code, Cursor, Windsurf): review and chat inside the editor, with the same review engine and a separate rate-limit bucket.
- **[D]** **Web app** (app.coderabbit.ai): Triage queue, Change Stack review workspace, Security dashboard, Plan/Coding Plan, Coding Agent, analytics, and settings.
- **[D]** **Slack agent** (launched 17 Apr 2026) and **Discord agent** (29 Jun 2026): investigate issues, produce implementation plans, open PRs from a thread. Slack uses "Scopes" — a named set of repositories, connections and spend limits that bound what the agent can touch in a given conversation.
- **[D]** **MCP server**: CodeRabbit exposes review/usage data via MCP for use inside other agents, and consumes external MCP servers as context.

### 2.4 The loop CodeRabbit is selling

**[V]** The implied loop is: agent writes code → CodeRabbit reviews it pre-PR (CLI/IDE) and at the PR → CodeRabbit explains and risk-scores the change (Change Stack) → CodeRabbit prioritises the queue (Triage) → CodeRabbit fixes what it found → CodeRabbit scans the repo and monitors post-merge (Security) → repeat. Every step terminates in a comment, a commit, or a PR.

---

## 3. Feature inventory by surface

### 3.1 Review (all paid plans; Free = summary only)

| Capability | Status | Notes |
| - | - | - |
| PR/MR summary + walkthrough | **[D]** | Changed-file summary, Mermaid sequence diagrams, 1–5 review effort, related issues/PRs, linked-issue assessment, suggested labels/reviewers, optional poem |
| Inline findings with severity | **[D]** | Line-level comments; severity taxonomy is not published as a fixed list |
| One-click fix of a finding | **[D]** | "Fix comments" (renamed from Autofix on 14 Oct 2026); free first turn |
| Agentic multi-file fix | **[D]** | "Autofix" (renamed from Autopilot on 14 Oct 2026); billed as agent minutes |
| Incremental review per push | **[D]** | Default on; pauses after 5 reviewed commits |
| Chat in PR / IDE / CLI | **[D]** | Separate chat allowance per plan |
| Learnings | **[D]** | Preferences captured from chat and applied to future reviews; viewable and editable |
| Code guidelines auto-detection | **[D]** | Reads `.cursorrules`, `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md` |
| Path instructions | **[D]** | Glob-scoped custom review rules |
| Multi-repo analysis | **[D]** | Detects cross-repo breaking changes; 0/1/5/10/20 linked repos by plan |
| Web search during review | **[D]** | For external context |
| Linked issues | **[D]** | GitHub, GitLab, Jira, Linear |
| 50+ third-party tools | **[D]** | Docs: "50+ third-party linters and security analysis tools"; almost all enabled by default, auto-selected by file type, run in sandboxed environments, many with 1-click fixes; structured output under "Review details"; per-tool YAML config or a repo config file is honoured |
| Review profiles | **[D]** | `Chill` (critical issues only, less noise) vs `Assertive` (comprehensive, incl. style/best practice) |
| Slop Detection | **[D]** | Flags low-quality AI-generated PRs (shipped 24 Mar 2026) |
| Pre-Merge Checks (built-in) | **[D]** | Docstring coverage 80% default, PR title, PR description, issue assessment |
| Custom Pre-Merge Checks | **[D]** | Team+; 10/20/20 by plan; ≤50-char name, ≤1000-char instructions; modes off/warning/error |
| Request-changes workflow | **[D]** | Bot can submit a "request changes" review where the platform allows || Docstring generation | **[D]** | Included from Essentials |
| Unit test generation | **[D]** | Team+ |
| Finishing Touches | **[D]** | Team+; fix CI, resolve conflicts, simplify, docstrings, tests, custom recipes (0/10/20/20) |
| Post-Merge Actions | **[D]** | Team+; changelog, tickets, notifications |
| Analytics / reports / data export | **[D]** | Usage and review analytics; scheduled reports; Data Export & Metrics API (14 Jan 2026) |
| Audit logs | **[D]** | Enterprise; shipped 25 Mar 2026 |
| SSO / SCIM | **[D]** | Enterprise |
| Custom RBAC | **[D]** | Enterprise; custom roles shipped 24 Feb 2026 |
| Multi-org administration | **[D]** | Enterprise; support-assisted setup |

### 3.2 Triage (Team+ only)

**[D]** A single cross-repository queue of open PRs ranked by what each needs next and what acting on it is worth. General availability 15 Sep 2026 on **GitHub Cloud only**; GitLab Cloud and self-managed GitLab in beta from 29 Sep 2026; **no Bitbucket support**; GitHub Enterprise Server is **not listed as a supported Triage provider** (the requirements page names GitHub Cloud and GitLab only), and Triage is **not available on self-hosted CodeRabbit deployments**. Requires an active Review seat — Triage is not sold standalone.

- **Priority:** P0–P3, with stated evidence: issue severity, linked Jira/Linear priority, and whether the PR blocks other work.
- **Views:** "Requires my action", "All PRs", "Close candidates", plus custom filters and saved views.
- **Actions:** review, repair, refresh, close, request reviewer, set priority, merge, follow.
- **Slack:** scheduled digest; reviewer requests; approve or close from Slack.
- **Triage rules** (28 Sep 2026, Team+): merge, close, auto-fix, and Slack-reminder rules. The changelog tags this for GitHub and GitLab, but the rules documentation states it is available to **GitHub Cloud** organisations only, with GHES limited to repository-label browsing in the rule editor. Treat GitHub Cloud as the supported surface and GitLab as unconfirmed.
- **[D] Rules can act without per-PR confirmation.** Templates: **Merge low-risk changes** (open non-draft, low risk, low review effort → merge using repository policy after required checks and reviews pass), **Auto-fix PRs** (Coding Agent repairs failed CI and review feedback, default **3** repair rounds, max 10, optional delay up to 720 hours), **Close stale PRs** (30 days no human activity → notice, wait **24 hours**, then close), **Send Slack reminders**, **Prioritize urgent fixes** (Slack reminder only; does not change priority).
- **[D] Guardrails on rules:** merge still obeys branch protection, required checks and permissions; a changed commit is rechecked and the action can be refused rather than substituted. Close posts a 24-hour notice and cancels on new activity. Conditions use current evidence, and **missing/stale evidence stays unknown and cannot authorize an action**. Merge, close and auto-fix cannot be combined in one rule. "Review matches" preview is required before enabling; acting on the existing backlog requires explicit selection, capped at 50 PRs. Pausing a rule does **not** remove a PR already accepted into GitHub's merge queue.
- **[D] Who can enable:** organisation admins only for merge/close/auto-fix; members can manage Slack-only rules within their repository access. Automated GitHub actions run under the **organisation's** CodeRabbit integration (not the viewing user's permissions); manual card actions run as the signed-in user.

**Correcting a common assumption:** CodeRabbit is often described as a product that "only comments" and never acts on its own. That is no longer accurate. A *Close candidate recommendation* never authorises closure, but an **enabled close rule can close matching PRs with no further per-PR confirmation**, and an enabled merge rule can merge low-risk PRs. CodeRabbit therefore has a narrow, rules-based autonomous action surface — bounded to **PR lifecycle operations on GitHub Cloud**, not production systems. Any comparison that says CodeRabbit "only comments" is out of date.

### 3.3 Explain / Change Stack (core in Essentials; AI chat and coding tasks Team+)

**[D]** A review workspace that reorganises a PR from a flat file list into **cohorts/layers** with range-specific summaries, semantic diff, and inline sequence diagrams or ERDs where useful. Includes:

- **Semantic diff** — distinguishes real edits from code movement.
- **Findings** view — what was found, merge readiness, what to fix first.
- **In-app review conversations** (15 Jul 2026).
- **All Reviews** view and **Code Peek**.
- **Architecture impact** — which system-level responsibilities and relationships changed (Advanced+).
- **Blast Radius** — downstream reach beyond changed lines (Advanced+).
- **Security Blast Radius** — how a PR changes dependencies, consumers, tests, and security-relevant paths (Advanced+).

**[V]** The page itself concedes the epistemic limit: it asks reviewers to weigh "what our agents can't deduce from the diff." Useful admission that intent reconstruction is partial.

### 3.4 Plan / Coding Agent / Engineer

- **[D] Coding Plan** ("CodeRabbit Plan"): a codebase-aware implementation plan generated from an issue or description, intended to be handed to any coding agent. Team+.
- **[D] Coding Agent** (`/code`): turns a request, a review finding, a coding plan, or a failing CI check into a cloud-implemented task, delivered as a commit or **stacked PR** for approval. Billed in **agent minutes**, separate from review plans.
- **[V] "CodeRabbit Engineer"** (pricing page): assign engineering tasks via Slack, ask about code/systems, run coding tasks, create automations that run autonomously or on demand; "Diagnose issues & outages". This is the closest thing to incident response in the product and is still bounded to producing code, tasks, and analysis.
- **[D] Agent skills** (23 Jul 2026): reusable skill definitions for the agent.

**Naming caution:** marketing uses "CodeRabbit Engineer" and "CodeRabbit Agent"; docs use "CodeRabbit Agent for Slack" and "Coding Agent" for the cloud implementation surface. These are related but not consistently the same object across pages. Do not treat "Engineer" as a separate SKU — it is on-demand and billed via agent minutes.

### 3.5 Security (Advanced for continuous PR review; Deep Scan usage-billed)

**[D]** Launched **9 Jul 2026** as a beta, GitHub-only at launch. Now: GitHub Cloud, GHES, GitLab Cloud and self-managed, Azure DevOps **Services**, Bitbucket Cloud and Data Center. **Azure DevOps Server is not supported.**

Capability set, as documented:

| Capability | Status | Plan |
| - | - | - |
| Security code review (static analysis, secrets, dependency, IaC checks) | **[D]** | Included from Essentials |
| Continuous PR security review with attack-surface visibility | **[D]** | Advanced/Enterprise |
| PR security review with severity, exploitability, reachability | **[D]** | Advanced/Enterprise |
| Security Architecture Review | **[D]** | Advanced/Enterprise |
| Security Blast Radius | **[D]** | Advanced/Enterprise |
| Attack-surface map | **[D]** | Shipped 4 Sep 2026 |
| AI Deep Scan (whole-repo, on-demand or scheduled) | **[D]** | Usage-based; listed for Essentials through Enterprise. Availability on the Free plan is not stated — the 1 Oct 2026 change covers **retrieving findings** on all plans, not running scans |
| Fix with AI | **[D]** | Free first turn |
| Security merge-blocking check | **[D]** | Shipped 5 Oct 2026; GitHub Cloud and GHES; publication off by default; default blocking threshold **Critical**; not merge-queue compatible |

- **Reachability tiers:** external, internal, unreachable (plus unknown for older findings). **Exploitability tiers:** trivial, moderate, difficult, theoretical. Severity is adjusted by these — external reachability no change; internal usually −1 level (capped at low for internal-only services); unreachable capped at low; difficult exploitability −1 level; theoretical capped at low.
- **How a scan works:** **Map** (inventory the repo, group into subsystems, map entry points, trust boundaries, controls, sinks, config) → **Investigate** (follow callers, trace untrusted input to sensitive operations, validate IaC misconfigurations) → **Verify** (reopen cited code, reject duplicates, mitigated, speculative, test-only, dead-code and unreachable candidates). Rule-based results are treated as *candidate signals*, not findings, until confirmed against repository evidence.
- **Finding categories:** authn/authz (authz bypass, IDOR, broken auth, CSRF); unsafe input/data handling (injection, XSS, SSRF, open redirect, insecure deserialization, path traversal); sensitive data and config (data exposure, info disclosure, CORS, weak crypto, DoS, misconfiguration); **AI/LLM application risks** (prompt injection, improper output handling, excessive agency); **IaC** (Terraform, OpenTofu, Kubernetes, Helm, Dockerfile, Containerfile, Bicep, CloudFormation, ARM, Compose).
- **Evidence per finding:** category, severity, CWE, repo/path/line range/subsystem, description, recommendation, reachability + exploitability + call stack where available, inline code context or permalink to the scanned commit, status, Fix with AI status.
- **[D] Two candid limits stated in the docs, both important for a Helix comparison:** (1) "CodeRabbit Security does not prove that a repository has no vulnerabilities"; (2) a cited commit "does not establish which version is deployed." The second is the clean admission that the product has **no deployment-state awareness** — it can tell you a vulnerable commit exists, not whether that commit is running in production.
- **AI Deep Scan:** separate usage billing; not included in Advanced or Enterprise. Up to 100 path excludes per repo (512 chars/path) and 100 path instructions per repo (4,000 chars each), repository context 4,000 chars, one weekly schedule per GitHub repo, custom scan branches GitHub-only, export to CSV/JSON/SARIF/PDF, API export capped at 50,000 records / 16 MB. Linked context repos max 20 per scan, and linked-repo context is GitHub Cloud, GHES and GitLab Cloud only — **not** self-hosted GitLab, Azure DevOps or Bitbucket. Since 1 Oct 2026 the findings API is open to all plans; since 2 Oct 2026 linked repos are supported in Deep Scan.
- **[D] Fix with AI opens a PR/MR and never merges it automatically.** Each fix runs as a Coding Agent task. "Share → For agents" produces self-contained Markdown with the finding and recommendation — an explicit hand-off format for another agent, which is the most direct integration point CodeRabbit offers to an incident-response tool.
- **[D] There is no SCA/SBOM product.** The docs state plainly: "AI Deep Scan does not perform dependency or software composition analysis (SCA) or generate a software bill of materials (SBOM)." "Dependency checks" in marketing means reviewing dependency-file changes, not continuous transitive inventory or licence analysis. This is a real gap versus Snyk/Semgrep-class tools and should not be glossed.
- **[V]** Security page repeats the vendor statistic that AI-written code contains "50% more vulnerabilities," linking to CodeRabbit's own report.

### 3.6 CLI and IDE

- **[D]** CLI v0.9.0, 8 Oct 2026, beta. Local review of uncommitted/staged changes and remote review of a branch. Remote review rejects **≥300 changed files**. Review time typically **7–30+ minutes** for CLI according to docs; PR review "typically longer than five minutes on most projects."
- **[D]** IDE extension for VS Code, Cursor, Windsurf. Configurable in-editor; same engine; separate rate bucket.
- **[V]** "Reduces PR comment noise by 80%" (undated marketing claim on the VS Code extension page). No methodology, no denominator.

### 3.7 Slack and Discord

- **[D]** Slack agent: investigate, plan, open PRs, answer codebase questions; scoped by repository set, connections, and spend limits per conversation; admin monthly spend cap.
- **[D]** Discord agent: same class of capability, launched 29 Jun 2026.
- **[D]** Slack digest for Triage; approve/close PRs from Slack.

---

## 4. Integrations and deployment

### 4.1 Git platforms

| Platform | Review | Triage | Security | Notes |
| - | - | - | - | - |
| GitHub Cloud | **[D]** | **[D]** GA | **[D]** | Fullest surface |
| GitHub Enterprise Server | **[D]** | **[U]** | **[D]** | Review support added 3 Jun 2026; Triage is documented for GitHub Cloud only, and GHES is not listed as a Triage provider |
| GitLab.com | **[D]** | **[D]** beta | **[D]** | |
| GitLab self-managed | **[D]** | **[D]** beta | **[D]** | Can connect to CodeRabbit Cloud; Triage unavailable on self-hosted CodeRabbit |
| Azure DevOps Services | **[D]** | **[D]** not supported | **[D]** | Azure DevOps **Server** unsupported for Security |
| Bitbucket Cloud | **[D]** | **[D]** not supported | **[D]** | Change Stack supported from 20 Jul 2026 |
| Bitbucket Data Center | **[D]** | **[D]** not supported | **[D]** | |

### 4.2 Other integrations

- **[D]** Issue trackers: Jira, Linear, GitHub Issues, GitLab Issues.
- **[D]** Chat: Slack and Discord agent surfaces; Slack also carries Triage digests and PR actions.
- **[D]** MCP: consumes external MCP servers as review context (5/10/15/20 by plan); exposes its own review/usage data via MCP.
- **[D]** REST API: review and MCP usage metrics, analytics, Deep Scan findings. Most endpoints Enterprise-gated; Deep Scan findings API open to all plans since 1 Oct 2026.
- **[D]** Marketplaces: GitHub Marketplace (verified publisher), AWS Marketplace, GCP Marketplace. Marketplace subscriptions are excluded from usage-based add-on billing and from INR billing. **[V]** The GitHub Marketplace listing displays **348,737 installs** and monthly prices of $30/$60/$90 as of October 2026 — install counts are platform-displayed, not audited, and include historical installs.
- **[D]** Self-hosted: **Enterprise only, 500+ user seats**. Supported on GHES, GitLab self-managed, Azure DevOps, Bitbucket Data Center. Bring-your-own LLM provider. A reverse-tunnel option avoids inbound network exposure. Self-hosted deployments can disable all retention.
- **[D]** Cloud regions: US-hosted by default; "company-selected SaaS regions" is an Enterprise custom-deployment option per pricing page, but the specific region list is not published.

### 4.3 What is notably absent

- **[D]** No production incident integration (PagerDuty, Opsgenie, incident.io, Datadog, ServiceNow ITOM) is documented as a first-class surface. Jira/Linear are issue trackers, not incident tools.
- **[D]** No observability/tracing ingestion (no Datadog/Grafana/Sentry telemetry as review context).
- **[D]** No runtime or deployment-state awareness. Review reasons from the repository, the diff, linked issues, and connected MCP context only.
- **[D]** No deployment rollback, feature-flag, or runbook execution capability.

---

## 5. Pricing (October 2026)

### 5.1 Plans

**[D]** Annual billing saves 20% versus monthly. Prices are per developer per month; annual prices are billed annually.

**Default trial:** new organisations get a **14-day Advanced trial** with Advanced features, trial review limits matching the selected plan, **300** files/PR, **20** custom recipes, **20** custom checks, **10** linked repos, **15** MCP servers, and **50** chat messages/hour. Trial overages are free.

| Plan | Annual | Monthly | Former name | Core inclusion |
| - | - | - | - | - |
| Free | $0 | $0 | — | Unlimited public + private repos; **PR summarisation only**; reviews via VS Code extension and CLI; 14-day Advanced trial |
| Open Source | $0 | $0 | — | Unlimited public repos; **Team features free**; separate rate-limit tier by stars; <10 stars requires manual trigger |
| Essentials | **$24** | **$30** | Pro | PR reviews, integrations, knowledge base, linters/SAST, analytics, docstrings, Fix comments, usage-based reviews |
| Team | **$48** | **$60** | Pro+ | Everything in Essentials + Triage, Plan, issue planning, unit tests, merge-conflict resolution, Finishing Touches, Post-Merge Actions |
| Advanced | **$72** | **$90** | (new) | Everything in Team + continuous PR security review, blast radius, architecture impact, security-informed triage |
| Enterprise | Contact sales | Contact sales | — | + self-hosting, multi-org, SSO, custom RBAC, audit logs, API access, SLA, CSM, marketplace billing, custom regions |

**[D]** INR-billed organisations can choose Essentials or Team only (no Advanced self-serve). After a trial expires without subscribing, the org reverts to Free with lower limits.

### 5.2 Feature gating that matters for a comparison

| Capability | Essentials | Team | Advanced | Enterprise |
| - | - | - | - | - |
| Triage | — | ✓ | ✓ | ✓ |
| Change Stack core | ✓ | ✓ | ✓ | ✓ |
| Change Stack AI chat + coding tasks | — | ✓ | ✓ | ✓ |
| CI fixer, issue planning, unit tests, merge conflicts | — | ✓ | ✓ | ✓ |
| Continuous PR security review | — | — | ✓ | ✓ |
| Blast Radius / Architecture Impact | — | — | ✓ | ✓ |
| Security-informed triage | — | — | ✓ | ✓ |
| AI Deep Scan | usage | usage | usage | usage |
| Custom Pre-Merge Checks | 0 | 10 | 20 | 20 |
| Custom Finishing Touch recipes | 0 | 10 | 20 | 20 |
| Linked repositories | 1 | 5 | 10 | 20 |
| MCP servers | 5 | 10 | 15 | 20 |
| Multi-org, SSO, custom RBAC, audit logs | — | — | — | ✓ |

**[D]** Public non-trial repositories on Essentials get an open-source exception of up to **10** custom Finishing Touch recipes and **10** custom Pre-Merge Checks. Private repos and trials use the standard 0.

**Commercial implication:** the "change management" story (Triage, blast radius, architecture impact, continuous security) is **Advanced ($72/$90)**, not the entry product. A comparison that prices CodeRabbit at $24 must state that Triage and security are excluded at that tier.

### 5.3 Usage-based reviews (add-on)

**[D]** Available to Essentials, Team, Advanced (not Free, not OSS, not Enterprise self-serve).

- **$1.00 per credit**, **4 reviewed files per credit**, i.e. **$0.25 per reviewed file**.
- Modes: **Automatic**, **On demand**, **Off**. Monthly spend cap available.
- Trial overages are **free**; the first eligible over-limit trial review is completed at $0 and the add-on is auto-enabled unless opted out.
- **PRs with more than 300 files cannot use usage pricing** at all.
- The interactive "Review on demand using usage pricing" action exists only on GitHub / GHES.
- Reviews continued with credits do **not** draw down the included allowance.
- INR billing and marketplace subscriptions are excluded.

### 5.4 Agent minutes (Coding Agent / Engineer / Autofix)

**[D]** **$0.40 per active agent minute** (~$24/hour), billed separately from review plans. Includes a **"$50/user free agent minutes"** trial grant. Free first turn for: Fix comments, Finishing Touches, Resolve merge conflicts, Security Fix with AI. Admin monthly spend cap available.

### 5.5 AI Deep Scan

**[D]** Usage-based, **not included** in Advanced or Enterprise. Optional free allowance. Priced separately from review credits. Because pricing is quoted per scan/usage in-app rather than on a public rate card, **the effective cost per scan is not publicly determinable** — mark as unverified for budgeting purposes.

### 5.6 Cost model worked example (illustrative, from documented rates)

A 20-developer team on **Advanced annual** = 20 × $72 = **$1,440/month** (~$17,280/year) before usage. If the team runs 2,000 reviewed files/month beyond allowance at $0.25/file, that is **+$500/month**. If it uses 100 agent minutes/developer/month, that is 2,000 × $0.40 = **+$800/month**. Agent and usage costs are therefore material relative to seat cost at high volume — seat price is a floor, not a total.

---

## 6. Rate limits and fair use

### 6.1 Included allowances (per developer, rolling hourly)

**[D]**

| Plan | PR/hr | IDE/hr | CLI/hr | Files per review | Chat/hr |
| - | - | - | - | - | - |
| Free | 1 (summary only) | 3 | 3 | 150 | N/A |
| OSS | 1–10 (by stars) | 1 | 3 | 100–300 | 25 |
| Essentials | 5 | 5 | 5 | 150 | 50 |
| Team | 8 | 8 | 8 | 300 | 75 |
| Advanced | 10 | 10 | 10 | 300 | 100 |
| Enterprise | 12 | 12 | 12 | 300 | 100 |

- **[D]** Grandfathered Pro+ → Team customers retain **10/hr**.
- **[D]** Allowances are rolling, not reset-on-the-hour: capacity returns as earlier reviews age out.
- **[D]** Enterprise 12/hr is a baseline; only new contracts explicitly enrolled in adaptive limits are subject to the bands below.
- **[D]** Limits are **per developer identity**, not per org. A CI bot or shared agent account that authors every PR concentrates all consumption on one identity — a documented anti-pattern.

### 6.2 Fair-use adaptive refill

**[D]** Sustained activity reduces the refill rate in bands. Reducing activity restores it over time.

**Essentials** (reviews in last 7 days → refill/hr):
0–29 → 5 · 30–39 → 4 · 40–49 → 3 · 50–59 → 2 · 60+ → 1 (one at a time)

**Team:**
0–39 → 8 · 40–49 → 6 · 50–59 → 4 · 60–69 → 2 · 70+ → 1

**Advanced:**
0–49 → 10 · 50–59 → 8 · 60–69 → 4 · 70–79 → 2 · 80+ → 1

**Enterprise (enrolled contracts only):**
0–59 → 12 · 60–69 → 10 · 70–79 → 5 · 80–89 → 3 · 90+ → 1

**Operational reading:** the headline "5/8/10/12 reviews per hour" is a ceiling that decays to **1/hour** under sustained use. The decay begins at roughly **30 reviews/week on Essentials**, **40/week on Team** and **50/week on Advanced** — i.e. about **4–7 reviews per developer per day** — and reaches the floor at **60/70/80 per week** (~9–11/day). For any team reviewing at normal engineering velocity, the headline number is not the throughput they will experience. This is the most under-reported limit in CodeRabbit's pricing.

### 6.3 What counts as a review event

**[D]** One event per **push**, not per commit:

| Action | Reviews used |
| - | - |
| PR opened | 1 |
| 3 commits pushed together | 1 |
| 3 commits pushed separately | 3 |
| Edit a file in GitHub web UI | 1 |
| "Update branch" or "Resolve conflicts" | 1 |
| `@coderabbitai review` / `full review` | 1 each |
| Force-push, rebase, reopen, draft→ready | 1 |
| Push that was rate-limited | 0 |
| Review continued with credits | 0 (billed as usage) |

**[D]** A review that is superseded by a new push still consumes its review. Docs note a review "takes longer than five minutes on most projects," so pushing inside that window discards and pays for a review. This is the single largest source of unexpected consumption.

### 6.4 What does not consume allowance

**[D]** Unpushed commits; pushes to a branch with no open PR; PRs excluded by auto-review filters; chat messages (separate limit); blocked pushes; paid continuations.

---

## 7. Supported languages and size limits

### 7.1 Language coverage

**[D]** There is **no canonical "supported languages" page**. The docs FAQ states: *"CodeRabbit works with all programming languages, with varying proficiency."* That is the only blanket statement.

**[D] Derived from tool configuration** (OpenGrep v1.30.1 and ast-grep v0.45.3 file-extension lists in the tool catalog):

- **OpenGrep:** C, C++, C#, Go, Java, JavaScript, TypeScript, Kotlin, Python, Ruby, Rust, PHP, Scala, Swift, Terraform, JSON.
- **ast-grep adds:** Bash, CSS, ERB, Elixir, Haskell, HTML, Lua, SQL, YAML.
- **Tool catalog also covers:** Fortran, Verilog/SystemVerilog, PowerShell, batch, Smarty, Prisma, Protobuf, GraphQL, Vue, Svelte, Astro, Jupyter notebooks.

**Practical reading:** first-class, pattern-based coverage is roughly **25–30 languages**; everything else relies on the LLM's general competence plus generic linting. Proficiency is not published per language, and there is no published per-language accuracy data. Treat "all languages" as a marketing statement and the tool list as the defensible coverage claim.

### 7.2 Size and throughput limits

| Limit | Value | Source |
| - | - | - |
| Files per review | 300 (Free/Essentials 150; OSS 100–300) | **[D]** |
| Files skipped by OpenGrep/ast-grep | ≥8 MiB | **[D]** |
| CLI remote review rejected | ≥300 changed files | **[D]** |
| Usage pricing for large PRs | Not available >300 files | **[D]** |
| Typical PR review time | "longer than five minutes on most projects" | **[D]** |
| Typical CLI review time | 7–30+ minutes | **[D]** |
| Deep Scan path excludes | 100 per repo (512 chars per path) | **[D]** |
| Deep Scan path instructions | 100 per repo (4,000 chars each) | **[D]** |
| Deep Scan repo context | 4,000 chars | **[D]** |
| Deep Scan weekly schedules | 1 per GitHub repo | **[D]** |
| Deep Scan API export | 50,000 records / 16 MB | **[D]** |
| Custom check name / instructions | ≤50 chars / ≤1000 chars | **[D]** |

---

## 8. Security, privacy, and compliance

### 8.1 Certifications and attestations

| Item | Status | Evidence |
| - | - | - |
| SOC 2 Type II | **[D]** | Announced in changelog 11 Mar 2024; reaffirmed in privacy policy (updated 16 Oct 2026). Marketing FAQ says "audited annually." Report itself is **not public** — available on request via Trust Center. |
| GDPR | **[D]** | Changelog 1 Apr 2024; privacy policy names EU and UK Article 27 representatives. US-hosted with SCCs and UK addendum for EU/UK transfers. |
| HIPAA | **[U]** | Mentioned **only** in privacy policy §12 as a data-storage statement. **No BAA, no HIPAA configuration guide, no trust-center confirmation found.** Do not claim HIPAA compliance. |
| ISO 27001 | **[U]** | Only secondhand sources (trust-center listing title, third-party directories). No primary CodeRabbit page confirmed it. |
| ISO 42001 / AI management system | **[U]** | No evidence found either way. |
| FedRAMP / StateRAMP | **[U]** | No evidence found. |

**[D]** Compliance reports are gated behind a Trust Center request flow (`trust.coderabbit.ai/resources?selectedSection=POLICIES`, KB article updated 3 Oct 2025). The trust center is JS-rendered and returns only "Vanta" to a text fetch — the full report set, subprocessor list, and DPA could not be read.

### 8.2 Data handling

- **[D]** Code is sent to **OpenAI and/or Anthropic** for review. Neither provider trains on customer code, per CodeRabbit's statement.
- **[D]** **Open-source projects are used for training** — an explicit carve-out in the privacy policy. This is a material difference between OSS and private-repo handling and is often missed.
- **[D]** Review cache is encrypted except for OSS; cache expires within **7 days**.
- **[D]** `knowledge_base.opt_out: true` irreversibly deletes stored knowledge-base data; self-hosted deployments can disable all retention.
- **[D]** Model-output logging is **off by default**; opt-in for eligible non-OSS organisations.
- **[D]** Servers are US-hosted; EU/UK transfers rely on SCCs plus the UK addendum.
- **[U]** Exact retention periods beyond the 7-day cache are not published. Subprocessor list is not publicly readable.
- **[U]** DPA terms and the self-hosted addendum could not be read (both pages render only "Loading legal document").

### 8.3 Product security posture

- **[D]** Self-hosted option keeps code in the customer's environment; BYO LLM provider; reverse tunnel avoids inbound exposure. Requires 500+ seats, so it is not available to small teams.
- **[D]** Enterprise audit logs (25 Mar 2026) record administrative and product actions.
- **[D]** Custom RBAC and SSO/SCIM at Enterprise.
- **[D]** Security check can block merges at a configurable severity threshold (default Critical), but is **GitHub Cloud and GHES only** and **not merge-queue compatible**.
- **[D]** No published threat model, pen-test summary, or vulnerability disclosure SLA beyond a `/vulnerability-disclosure` page.
- **[U]** No public CVE history or security advisory feed was found for CodeRabbit itself.

### 8.4 The compliance story in one line

CodeRabbit can produce a SOC 2 Type II report and a GDPR posture on request, is US-hosted with SCCs, and will self-host for large enterprises. It does **not** publicly evidence HIPAA, ISO 27001, or ISO 42001, and its subprocessor/DPA details are behind a gated trust center. For a regulated buyer, the gating itself is a diligence item.

---

## 9. 2026 change timeline

**[D]** All entries from `docs.coderabbit.ai/changelog` (271 dated entries as of 19 Oct 2026) unless marked **[V]**.

| Date | Change | Why it matters |
| - | - | - |
| 14 Jan | Data Export & Metrics API | First programmatic analytics surface |
| 18 Feb | Multi-Repo Analysis | Cross-repo breaking-change detection |
| 24 Feb | Custom roles | Enterprise RBAC |
| 26 Feb | Autofix (now "Fix comments") | One-click finding fixes |
| 24 Mar | Slop Detection | Flags low-quality AI-generated PRs |
| 25 Mar | Audit Logs | Enterprise compliance surface |
| 16 Apr | Pro+ plan; Global Overrides | Packaging change precursor |
| 17 Apr | Slack Agent | Chat-surface entry point |
| 7 May | Change Stack | Layered PR review workspace |
| 29 May | Usage auto-refill cap | Spend control |
| 3 Jun | GHES + GitLab review support | Platform expansion |
| 8 Jun | Lite / Pro Legacy retired | Forced migration |
| 15 Jun | Plans renamed Essentials / Team / Advanced | Current packaging |
| 20 Jun | ast-grep language expansion | +9 languages |
| 9 Jul | **CodeRabbit Security** (beta, GitHub only); Post-Merge Actions | Entry into security |
| 15 Jul | Large-PR file limits; in-app review conversations | Throughput + collaboration |
| 20 Jul | Change Stack on Bitbucket | Platform parity |
| 23 Jul | Agent skills | Reusable agent behaviours |
| 12 Aug | "Agentic Change Management" framing; **$143M Series C at $1.5B** **[V]** | Positioning + capital |
| 21 Aug | AI Deep Scan API | Programmatic findings |
| 4 Sep | Attack-surface map | Security depth |
| 15 Sep | **Triage GA** | Queue product ships |
| 28 Sep | Triage rules | Automation over the queue |
| 28 Sep | Sonnet 5.5 model benchmark **[V]** | Model swap claim (see §10) |
| 29 Sep | Triage for GitLab (beta); OpenAI Marketplace listing **[V]** | Platform expansion |
| 1 Oct | Deep Scan findings API open to all plans | Security findings retrievable without an Enterprise plan |
| 2 Oct | Deep Scan linked repositories | Context depth |
| 5 Oct | Security merge-blocking check | The only **security** gate CodeRabbit ships |
| 6 Oct | Security agent benchmark **[V]** | Competitive claim (see §10) |
| 8 Oct | CLI v0.9.0 | Pre-PR surface matures |
| 13 Oct | Cursor Origin launch partner **[V]** | Distribution |
| 14 Oct | **Rename: Autofix → "Fix comments"; Autopilot → "Autofix"** | Naming churn; marketing pages lag |

**Reading the timeline:** 2026 is a year of **surface expansion, not review-engine reinvention**. The review product existed before; the new surface area is triage, change explanation, security, planning, and agent execution. Nearly all of it terminates in a PR. The only pre-merge security gate is the 5 Oct check; the only autonomous lifecycle actions are the 28 Sep Triage rules.

**Naming churn warning:** the 14 Oct rename means "Autofix" in any pre-14-Oct document refers to what is now "Fix comments." The current pricing page still says "Autofix — One-click code changes for unresolved findings," which under the new taxonomy describes **Fix comments**, not Autofix. Cite the changelog date alongside any use of these terms.

---

## 10. Quantified vendor claims

All items are **[V]** unless noted. None are independently audited. Use with the caveat column.

| Claim | Number | Date | Caveat |
| - | - | - | - |
| Customers | "17,000+" / "17K" | 2026, multiple pages | No definition of "customer" (paid seat? org? trial?) |
| GitHub Marketplace installs | 348,737 | displayed Oct 2026 | Installs ≠ active orgs; includes trials and uninstalls over time |
| OSS reviews in Sept 2026 | 1,049,817 (+14.4% MoM) | 8 Oct 2026 blog | Self-reported; 47,585 on 30 Sep alone; monthly series 421,336 (Apr) → 826,999 (Jul) → 917,439 (Aug) → 1,049,817 (Sep) |
| Public repos reviewed | ">300,000" | 2026 | Self-reported |
| OSS commitment | ">$10M over 12 months"; $1.3M counted as of 8 Oct 2026; earlier $1.2M cash | 2026 | Mixes cash and in-kind; counting method undisclosed |
| AI code issue rate | "1.7x more issues"; also "50% more vulnerabilities"; also "41% more bugs" | 17 Dec 2025 report | Three inconsistent framings of the **same 470-PR study** (320 AI-co-authored, 150 human). AI authorship inferred from co-author trailers, not confirmed. Pick one framing and cite the report, not the page. |
| Security benchmark | CodeRabbit **86.7%**, Devin 73.3%, Codex Security Plugin 62.0%, Claude Security 40.0% | 6 Oct 2026 blog | Internal benchmark: 100 vulns, 94 repos, 11 languages; advisory withheld; vendor acknowledges training exposure. **Not independent.** |
| Model benchmark (Sonnet 5.5) | 6/13 hardest cases vs 4/13; 41.2% vs 40.0% precision; 6:33 vs 13:31 mean review time; $0.46 vs $1.16 per review | 28 Sep 2026 blog | Vendor-run; 13 cases is a tiny sample; cost figures are vendor-computed |
| VS Code noise reduction | "reduces PR comment noise by 80%" | undated | No methodology, no denominator, no baseline |
| Review effort scale | 1–5 | docs | This is a product feature, not a claim |
| Security coverage | "every repo and PR" | pricing page | Contradicted by tier gating: continuous security is Advanced+ only |

**Claims to avoid repeating without qualification:** "17,000 customers" (undefined), "80% less noise" (no method), "50% more vulnerabilities" (one framing of a contested internal study), and any security benchmark percentage (internal, non-independent, advisory withheld).

**Independent evidence** (Signal65, Martian, the $143M round) is catalogued in `landscape_evidence.md` §3 and should be cited from there rather than re-derived here.

---

## 11. Unverified / could not confirm

| Item | Why it matters | Status |
| - | - | - |
| ISO 27001 certification | Common enterprise RFP requirement | Only secondhand sources; no primary confirmation |
| HIPAA BAA availability | Blocks healthcare buyers if absent | Privacy policy mentions HIPAA once; no BAA or config guide found |
| Full subprocessor list | GDPR/vendor-risk diligence | Trust center JS-gated; not readable |
| DPA terms | Contractual data-processing obligations | `/legal/dpa` renders "Loading legal document" only |
| Self-hosted addendum terms | Determines on-prem obligations | `/legal/self-hosted` renders loading state only |
| Data retention periods beyond 7-day cache | Privacy impact assessment | Not published |
| AI Deep Scan unit price | Budgeting | Usage-based; no public rate card |
| Triage on GHES / Azure DevOps / GitLab self-managed | Platform fit | Only GitHub Cloud GA and GitLab beta documented |
| Per-language accuracy | Language-risk assessment | Not published; "all languages, varying proficiency" only |
| Effectiveness of Triage routing | Core value claim | No public data; see `landscape_evidence.md` gaps |
| Effectiveness of Change Stack / Security | Core 2026 claims | No independent evaluation found |
| CodeRabbit's own CVE / incident history | Security diligence | No advisory feed found |
| Exact "customer" definition behind 17,000+ | Sizing the installed base | Not defined |
| Whether CodeRabbit can act on production systems | Central comparison question | No documentation of any production actuation; treat as **no** |

---

## 12. Source list

All fetched 17–19 October 2026 unless noted. **[D]** = documentation of record; **[V]** = vendor marketing/blog; **[S]** = secondary/independent.

**Documentation (`docs.coderabbit.ai`)**
- `/management/plans` — plans, feature gating, rate-limit table, feature limits **[D]**
- `/management/rate-limits` — review events, refill mechanics, fair-use bands, anti-patterns **[D]**
- `/management/usage-based-addon` — credits, pricing, modes, exclusions **[D]**
- `/configuration/*` — full configuration reference, auto-review controls, path instructions **[D]**
- `/pr-reviews/*` — review overview, walkthroughs, pre-merge checks, custom checks **[D]**
- `/security/*` — security index, PR check, attack surface, blast radius, access & billing **[D]**
- `/triage/*` — prioritisation, FAQ, rules **[D]**
- `/change-stack` — cohorts, semantic diff, architecture impact **[D]**
- `/code` and `/code/access-and-billing` — coding agent, agent minutes **[D]**
- `/slack-agent` and `/slack-agent/scopes` — Slack surface **[D]**
- `/knowledge-base/*` — multi-repo analysis, learnings, connections **[D]**
- `/tools/*` — tool catalog, OpenGrep and ast-grep language lists **[D]**
- `/platforms/*` — platform support matrix **[D]**
- `/self-hosted/*` — self-hosted requirements **[D]**
- `/changelog` — 271 dated entries **[D]**
- `/llms.txt` — documentation index

**Marketing (`coderabbit.ai`)**
- `/` — positioning, "17K customers" **[V]**
- `/pricing` — monthly prices, per-tier feature marketing, on-demand products **[V]**
- `/enterprise`, `/customers`, `/about-us` — customer counts, logos **[V]**
- `/security`, `/review`, `/triage`, `/change-stack`, `/agent` — product marketing **[V]**
- `/privacy-policy` (updated 16 Oct 2026) — SOC 2, GDPR, HIPAA mention, OpenAI/Anthropic, OSS training, cache, hosting **[D/V mixed — policy is a legal statement, treat as documented]**
- `/faq` — answers JS-collapsed; only visible text usable **[V]**
- `/blog/security-agent-benchmarks` (6 Oct 2026) — internal security benchmark **[V]**
- `/blog/one-million-open-source-reviews-september-2026` (8 Oct 2026) — OSS volume **[V]**
- `/blog/sonnet-5-5-model-review` (28 Sep 2026) — model benchmark **[V]**
- `/blog/state-of-ai-vs-human-code-generation-report` (17 Dec 2025) — 470-PR study **[V]**

**Secondary**
- SiliconANGLE, 12 Aug 2026 — $143M Series C at $1.5B, "Agentic Change Management" framing **[S]**
- Signal65, March 2026 — independent-adjacent benchmark (see `landscape_evidence.md`) **[S]**
- Martian Code Review Bench — ~300k PR adoption benchmark (see `landscape_evidence.md`) **[S]**

**Could not retrieve (JS-gated or 404)**
- `trust.coderabbit.ai` (all paths) — returns "Vanta" shell only
- `/compliance`, `/subprocessors`, `/api/compliance`, `/api/trust-center` — JS shell
- `/legal/dpa`, `/legal/self-hosted` — loading state only
- `docs.coderabbit.ai/security/data-privacy`, `/security/data-retention` — 404
- `docs.coderabbit.ai/tools/language.md` — 404
- `trustlists.org/company/code-rabbit/` — HTTP 429

---

## 13. Comparison hooks for the Helix report

Short list of the axes on which CodeRabbit and a production incident-response tool actually differ. Each is supported by the sections above; the analysis belongs in the report, not here.

1. **Actuation boundary.** CodeRabbit's maximum action is a commit, a PR, or a rules-based PR merge/close (§1, §3.2, §3.4). Helix-class systems act on running systems. This is a difference in kind, not degree, and it determines the safety bar.
2. **Trigger.** CodeRabbit is triggered by a push or a chat request. Incident response is triggered by an alert or an SLO breach. Neither product observes the other's trigger natively (§4.3).
3. **Evidence base.** CodeRabbit reasons over the diff, repository, linked issues, and MCP context. It has no telemetry, traces, logs, deployment state, or runtime topology (§4.3). Any claim that it "diagnoses outages" is bounded to code-level inference.
4. **Time horizon.** Review is pre-merge and asynchronous (minutes to hours). Incident response is in-incident and synchronous (seconds to minutes). CodeRabbit's documented review latency — "longer than five minutes on most projects", CLI 7–30+ minutes — is disqualifying as an incident surface (§7.2).
5. **Reversibility.** A bad review comment costs reviewer attention; a bad production action costs availability. CodeRabbit inherits branch protection, CODEOWNERS, and revert. An incident agent must supply its own gates.
6. **Overlap.** Both claim change-risk scoring, blast-radius analysis, and change-to-impact reasoning. CodeRabbit's versions are **pre-merge, static, and Advanced-tier**; a credible comparison should test them on the same change population rather than compare feature names.
7. **Commercial shape.** CodeRabbit is per-seat with decaying hourly limits and separate usage/agent billing (§5–§6). Incident tooling is typically per-host, per-incident, or per-agent-minute. Cost comparisons must normalise units.
8. **The honest framing.** These are **complementary control points on one change lifecycle** — pre-merge prevention versus post-deploy detection and remediation — with genuine overlap in change-risk and blast-radius reasoning and a hard boundary at production actuation. The report should say that plainly rather than manufacture a winner.

---

*End of fact sheet. Every factual item above is either documented, attributed as a vendor claim, or explicitly marked unverified. If a number is needed that is not here, it was not found in a primary source as of 19 October 2026.*
