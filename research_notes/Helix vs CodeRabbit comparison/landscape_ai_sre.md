# AI Incident Response / AI SRE Landscape and Human-in-the-Loop Safety (as of October 2026)

Research date: 19 October 2026. Scope: incident response and production change safety only; pre-merge code review is out of scope except where it defines the boundary. Vendor material is flagged. "Evidence" means a benchmark, independent study, or disclosed production metric with methodology — not a vendor case-study quote.

## Q1. Which products matter, what do they automate, and which execute production changes?

### Takeaway
The market has three layers: incident-management platforms adding AI investigation (PagerDuty, incident.io, Rootly); dedicated "AI SRE" agents that are read-only or approval-gated by default (Cleric, Traversal, Resolve AI, NeuBird, Ciroos); and a few products that actually mutate production (Azure SRE Agent, Harness rollback, Shoreline runbooks, plus PagerDuty/Resolve/NeuBird behind approval). Gartner's January 2026 Market Guide makes "autonomous, semiautonomous AI SRE decision making and actions **with humans in the loop**" mandatory — an accurate description of buyer expectations, not autonomous operations.

### Cited Findings

| Product | Automates | Executes production change? | Guardrails / evidence |
|---|---|---|---|
| **PagerDuty AI Agents** | AIOps alert grouping; triage, diagnosis, coordination, PIR; approved remediation | Approved only, via orchestration | AI credits on Professional+; "built-in guardrails"; no command-level detail ([PagerDuty](https://www.pagerduty.com/platform/ai-agents/), 19 Oct 2026 — vendor) |
| **incident.io Investigations** | Agentic RCA across telemetry, code changes, history; blast radius | **No.** "The only change Investigations can make to your systems is a pull request you review and merge yourself" | Daily backtests; adversarial hypothesis check; audit trail; SOC 2 ([incident.io](https://incident.io/investigations), 2026 — vendor) |
| **Rootly AI SRE** | Deploy/commit correlation, confidence-scored RCA, draft PR, rollback proposal, retros | No; PR/rollback is a proposal | HITL; no customer-data training; SOC 2 ([Rootly](https://rootly.com/ai), 19 Oct 2026 — vendor) |
| **Cleric** | Tracks PRs into prod, 14-day verification, regression RCA, fix PR, page ack | No; fix is a reviewed PR | Read-only default; SOC 2 Type II; list price ([Cleric](https://cleric.ai/product), [pricing](https://cleric.ai/pricing), Oct 2026 — vendor) |
| **Traversal** | Causal world model, triage, RCA, draft fixes; Workers GA 2 Sep 2026 | Claims resolution; "read-only defaults," BYOC/BYOM | 38–80% MTTR cuts; 255-Elo on 25 incidents, no paper ([Traversal](https://www.traversal.com/blog); [Business Wire 2 Sep 2026](https://www.businesswire.com/news/home/20260901532072/en/Traversal-Makes-Incident-Workers-Generally-Available-and-Launches-Alert-Workers) — vendor) |
| **Resolve AI** | On-call triage, parallel hypothesis RCA, postmortems | Approval-gated; engineers "make the final call on high-severity issues" | Customer-defined autonomy boundary ([Resolve AI](https://resolve.ai/product/ai-sre), 19 Oct 2026 — vendor) |
| **NeuBird** | Prevent/resolve/operate; <5-min RCA, offending commit, staged fix; agent governance | Yes in "Act" mode, through customer tooling, with human approval on every write | Suggest/Recommend/Act; SOC 2 Type II trail; claims 94% RCA accuracy ([NeuBird](https://www.neubird.ai/), Oct 2026 — vendor) |
| **Azure SRE Agent** | Detect, investigate, mitigate, verify; GitHub repair items | **Yes.** ReadOnly / Review / Autonomous | Blocks `delete`/`remove` and `az keyvault`; respects locks; validates subscription IDs; audit trail ([Microsoft Learn](https://learn.microsoft.com/en-us/azure/sre-agent/execute-mitigations), 18 Mar 2026 — vendor) |
| **Google Cloud Assist** | Investigations, proactive alert RCA, next steps | Limited; **Preview** as of 13 Oct 2026 | Agent service identity, least privilege, audit logging ([Google Cloud](https://cloud.google.com/gemini/docs/cloud-assist/overview), 13 Oct 2026 — vendor) |
| **Amazon DevOps Guru** | ML anomaly detection, insights, recommendations | No | **Retiring:** no new customers after 29 Oct 2026; access ends 30 Sep 2027 ([AWS](https://docs.aws.amazon.com/devops-guru/latest/userguide/welcome.html), 19 Oct 2026) |
| **Harness** | Deployment verification, anomaly detection, auto-rollback; AIDA summaries | **Yes** — rollback on failed verification (AI Verify is Beta) | Canary/blue-green; 1σ/2σ/3σ; fail-fast/fail-on-no-analysis ([Harness](https://developer.harness.io/docs/continuous-delivery/verify/verify-deployments-with-the-verify-step/), 19 Oct 2026 — vendor) |
| **Shoreline.io** | Runbook automation and fleet remediation | **Yes** — approved runbooks | `approvers`, `allowed_entities`, resource scope, audit ([Terraform docs](https://registry.terraform.io/providers/shorelinesoftware/shoreline/latest/docs/resources/runbook), 2025). Reportedly acquired by Nvidia (May 2026) — **unconfirmed** |

[AI SRE Landscape](https://aisre.com/landscape.md) (Oct 2026) is the best map, though vendor-maintained. Gartner's 26 Jan 2026 Market Guide lists 28 representative vendors including Azure, Harness, NeuBird, PagerDuty, Resolve, Rootly, Traversal, and incident.io ([reprint](https://www.gartner.com/doc/reprints?ct=260204&id=1-2MT3AR1H&st=sb), Jan 2026).

### Inferences
- The category has converged on **diagnosis as the product and execution as a policy setting**. Read-only is the default because it is the only way to sell to risk-averse buyers.
- Guardrails are standardizing on run modes, command deny-lists, resource locks/RBAC, and audit logs. Azure documents the most; most competitors document little. AWS retiring DevOps Guru signals detection-only AIOps is being absorbed into platforms.

### Gaps
- Only Cleric publishes list pricing ($700–$3,000/month, credit-based); PagerDuty sells AI credits but not a public AI SKU price; the rest are quote-only.
- No vendor except Azure documents which commands are blocked or what happens when an action fails.

## Q2. What evidence exists for autonomous remediation?

### Takeaway
Rigorous evidence is far more sober than demos. On live, high-fidelity benchmarks, frontier agents resolve roughly **15–30% of realistic compound failures end-to-end**, even when diagnosis alone looks like 40–70%. On easier single-fault scenarios ported from older benchmarks, success rises above 60% — which is why vendor demos look good. No commercial AI SRE product has been independently evaluated on a shared benchmark.

### Cited Findings
- **ITBench** (IBM Research + UIUC, arXiv 2502.05352, 7 Feb 2025): 94 scenarios; state-of-the-art agents resolved **13.8% of SRE**, 25.2% CISO, 0% FinOps. A later 102-scenario revision reports **11.4% SRE** — [arXiv](https://arxiv.org/abs/2502.05352); [Illinois Experts](https://experts.illinois.edu/en/publications/itbench-evaluating-ai-agents-across-diverse-real-world-it-automat/).
- **ITBench-AA** (Artificial Analysis + IBM, 27 May 2026): 59 Kubernetes RCA tasks on offline snapshots. Best model scores **56.2% average precision at full recall**; all models were below 50% at launch — [Artificial Analysis](https://artificialanalysis.ai/evaluations/itbench-aa), 19 Oct 2026. Diagnosis only, not mitigation.
- **AIOpsLab** (Microsoft Research, MLSys 2025; arXiv 2501.06706): fault injection over DeathStarBench. ReAct baseline averaged **28%** task success; a verification wrapper (RIVA, arXiv 2603.02345, 4 Mar 2026) raised it to **43.8%** — [AIOpsLab](https://www.microsoft.com/en-us/research/blog/aiopslab-building-ai-agents-for-autonomous-clouds/); [RIVA](https://arxiv.org/pdf/2603.02345). Mitigation remains the weak stage.
- **SREGym** (UIUC/Toronto, arXiv 2605.07161v2, 13 May 2026): strongest current benchmark — 90 problems, hardware/OS faults, ambient noise, metastable and concurrent failures. With noise: diagnosis **38.9–62.6%**, mitigation **57.3–76.3%**, end-to-end **30.4–53.7%** (without noise, up to 72.6/78.5/60.7%). On failures unique to SREGym, E2E collapses to **15.4–28.2%** without noise and **10.3–17.9%** with noise; ported ITBench/AIOpsLab problems reached 63.7% — [SREGym](https://arxiv.org/html/2605.07161v2). Benchmark design explains most "success."
- **Causely white paper** (arXiv 2605.18327, 18 May 2026): on a healthy baseline, some agents **hallucinated an incident 67% of the time** — [Causely](https://arxiv.org/pdf/2605.18327v1). Vendor-authored but a rare quantified failure-mode measurement.
- **Production claims**: Microsoft reports 1,300+ internal Azure SRE agents, **35,000+ incidents mitigated**, and App Service time-to-mitigation from a **40.5-hour human-only average to 3 minutes** — [Microsoft](https://techcommunity.microsoft.com/blog/appsonazureblog/how-we-build-and-use-azure-sre-agent-with-agentic-workflows/4508753), 2026. Self-reported, non-randomized, undefined baseline. Traversal claims a 255-point Elo gap on 25 incidents — [Traversal, 14 Aug 2026](https://www.traversal.com/blog), no paper. *Vendor claim.*

### Inferences
- The **diagnosis-to-remediation gap** is the defining number: a tool can be right about what broke and still fail to fix it safely. Demand separate precision and resolution metrics.
- Task composition matters more than model choice. Ported single-fault scenarios inflate results 2–4× versus compound, noisy, low-level failures. The credible 2026 value is **MTTR compression through faster evidence assembly**, which needs no write access.

### Gaps
- No independent evaluation of any commercial product on a shared benchmark; all cross-vendor comparisons are vendor-authored.
- No vendor discloses harmful-remediation, false-RCA, or self-rollback rates; benchmarks do not measure blast radius or recovery after a bad automated change.

## Q3. What do SRE and change-management practice say about safe automated change?

### Takeaway
The doctrine is consistent from 2016–2026: change is the dominant cause of outages, so change must be small, gradual, observable, reversible, and attributable. Automation is acceptable when bounded by the same controls as a human change — independent approval, blast-radius limits, canary analysis, rollback, and audit. SOC 2 and the EU AI Act add documentation and, for critical infrastructure, human-oversight obligations. None bless fully autonomous production mutation.

### Cited Findings
- **Google SRE**: "roughly **70% of outages are due to changes in a live system**"; best practice automates gradual rollout, canarying, and rollback — [Google SRE Book](https://sre.google/sre-book/introduction/), 2016/2017. A rule of thumb, not a controlled study.
- **DORA 2025** (~5,000 respondents, 23 Sep 2025): 90% use AI at work and >80% report productivity gains, but **AI adoption continues to have a negative relationship with software delivery stability**; 30% report little or no trust in AI-generated code. DORA advises fixing testing, version control, and feedback loops before scaling AI — [DORA 2025](https://dora.dev/research/2025/dora-report/); [Google Cloud](https://cloud.google.com/blog/products/ai-machine-learning/announcing-the-2025-dora-report), 23 Sep 2025.
- **SOC 2 CC8.1** requires documented change authorization, testing, approval, and segregation of duties — [AICPA Trust Services Criteria, 2022 revision](https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022). Auditors test that production changes map to an approved change record; an agent's action must be attributable to one.
- **Four-eyes / self-approval**: GitHub can require reviews, **code-owner** approval, dismiss stale approvals, and require approval from someone other than the last pusher. But **admins bypass protections by default** unless bypass is disabled — [GitHub Docs](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches), 19 Oct 2026. If an agent opens and approves its own PR, four-eyes collapses.
- **Blast radius / progressive delivery**: Harness AI Verify automatically triggers rollback on anomaly, with 1σ/2σ/3σ sensitivity, fail-fast thresholds, and fail-on-no-analysis — [Harness](https://developer.harness.io/docs/continuous-delivery/verify/verify-deployments-with-the-verify-step/), 19 Oct 2026. The safety property is **automatic reversal on failed analysis**, not autonomous forward change.
- **EU AI Act**: Regulation (EU) 2024/1689 entered into force 1 Aug 2024 and became applicable **2 Aug 2026**; GPAI obligations applied from 2 Aug 2025. After the AI Omnibus (in force 27 Jul 2026), high-risk Annex III obligations — including AI safety components in critical infrastructure — apply from **2 Dec 2027**, and Annex I products from **2 Aug 2028**. High-risk systems require risk management, logging, human oversight, and conformity assessment — [European Commission](https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai), updated 3 Aug 2026. Most AI SRE for SaaS is not automatically high-risk; agents operating critical-infrastructure safety components may be.

### Inferences
- The safety case is not "the model is accurate"; it is "the action is reversible, bounded, observable, and attributable." Restart/scale/rollback in a canary is defensible; schema migrations, IAM changes, and data deletion are not. Four-eyes is the control most threatened by AI: "agent proposes, human approves" should be a hard boundary, not a configurable default.
- The Act is now applicable, but the critical-infrastructure high-risk deadline slipped to Dec 2027. Buyers should not assume "AI SRE = high-risk," nor assume "no regulation."

### Gaps
- No authoritative source quantifies failure rates of AI-generated versus human changes; DORA measures outcomes, not actor type.
- The 70% figure is old and Google-specific, and SOC 2 has no AI-specific criteria; auditors are extrapolating CC8.1.

## Q4. Where does pre-merge review stop, and does anyone span both?

### Takeaway
The boundary is the **deploy event**. Pre-merge review asks "is this change correct, owned, and safe to ship?" Incident response asks "what is broken in production and how do we restore service?" A few products cross the line by tracking a PR into production and correlating regressions with that change — Cleric most explicitly, plus Harness, Rootly, incident.io, and Azure SRE Agent — but none feeds incident outcomes back into pre-merge review policy. That is the market gap.

### Cited Findings
- **Cleric** is the clearest span: it "analyzes your PR, then tracks its deployment to check that production behaves as you intended," verifies for up to 14 days, and opens a fix PR on regression — [Cleric](https://cleric.ai/product), Oct 2026. Post-merge change verification, not pre-merge review, but it closes the change-to-production-effect loop.
- **incident.io Investigations** reasons across "telemetry, deployments, code, and incident history"; a customer describes it drilling into "what changed in that pull request" — [incident.io](https://incident.io/investigations), 2026. Code-change correlation is an incident feature, not a review gate.
- **Rootly AI SRE** correlates "live telemetry, recent deploys, commits, and config changes," identifies the PR that introduced a bug, and drafts a fix PR — [Rootly](https://rootly.com/ai), Oct 2026. Incident-side correlation again.
- **Harness** makes the deploy and its verification one record, so a bad change is rolled back in the same pipeline — [Harness](https://developer.harness.io/docs/continuous-delivery/verify/verify-deployments-with-the-verify-step/), Oct 2026. Tightest change-to-remediation loop, but deployment safety, not incident RCA.
- **Azure SRE Agent + GitHub Copilot** is the closest closed loop: diagnosis creates a GitHub issue/repair item, Copilot opens a PR, and branch protection/CODEOWNERS still gate merge — [Azure SRE Agent](https://learn.microsoft.com/en-us/azure/sre-agent/overview), 19 Oct 2026.
- **Missing**: no mainstream product correlates a specific PR's diff and review comments with the probability that it causes a later incident, then tightens review policy. CODEOWNERS encodes ownership, not risk.

### Inferences
- The seam is the **commit SHA + deploy event + service catalog**. Most tooling failures happen there, not inside either product.
- A product spanning both needs read access to code, CI/CD, deploy events, telemetry, and incident history — a large security review, which is why adoption is slow. The missing feature is **change-risk memory**: using incident outcomes to change review requirements.
### Gaps
- No public data on how often AI incident tools correctly identify the causal PR versus a correlated but innocent change.
- No product documentation found that feeds incident causality back into code-review requirements.

## Q5. How do buyers and practitioners judge these tools?

### Takeaway
Buyers are optimistic about investigation and skeptical about autonomy. Enterprise adoption is forecast to grow from <5% in 2025 to 85% by 2029, but the requirement is human-in-the-loop decision-making, not lights-out operations. Practitioners' objections are hallucinated root causes, automation bias, permission sprawl, and vendors benchmarking on curated incidents. The best buying signal is a read-only shadow pilot measured against human RCA.

### Cited Findings
- **Gartner Market Guide** (26 Jan 2026): "By 2029, 85% of enterprises will use AI SRE tooling … up from less than 5% in 2025." Gartner lists "autonomous, semiautonomous AI SRE decision making and actions **with humans in the loop**" as mandatory — [Gartner reprint](https://www.gartner.com/doc/reprints?ct=260204&id=1-2MT3AR1H&st=sb); [Cast AI summary](https://cast.ai/gartner-market-guide-for-ai-sre-tooling), Jan 2026. Paywalled; reliable but second-hand.
- **Dynatrace survey** (919 IT leaders, Aug 2026): 67% of SREs say monitoring AI models is their top use case; **50% use AI-powered tools for automated incident response**; but AI is **less effective at cutting costs and reducing MTTR** than at other outcomes — [ITBrief](https://itbrief.com.au/story/sre-teams-take-on-wider-ai-oversight-in-production), 25 Aug 2026. Vendor-commissioned; large sample.
- **Independent practitioner view**: an Anthropic engineer who did on-call for Claude's serving stack says LLMs are "superhuman" at observing logs/traces but still struggle with **causation versus correlation**; a confident "root cause" is often "three possible issues," and the near-term win is generic mitigations and obvious rollbacks — [InfoQ](https://www.infoq.com/presentations/claude-sre-incidents), 26 Aug 2026.
- **Hallucinated diagnoses**: Causely measured a **67% false-positive/hallucinated-incident rate** on a healthy baseline for some agents — [Causely](https://arxiv.org/pdf/2605.18327v1), 18 May 2026. Vendor-authored but one of the few quantified failure-mode studies.
- **Practitioner skepticism**: an Ask HN thread asks "Anyone tried using AI SREs? What's missing?" and describes the desired product as read-only monitoring plus autonomous investigation — [Hacker News](https://news.ycombinator.com/item?id=45939408), 2025. A 2026 Show HN launches "Nightwatch, the open-source, read-only AI SRE," explicitly because write access is the trust barrier — [Hacker News](https://news.ycombinator.com/item?id=48438180). Anecdotal but consistent.
- **Pricing and adoption**: Cleric publishes Team $700/mo (1,400 credits), Pro $1,500, Scale $3,000; $0.50/credit monthly, $0.40 annual; 2 credits per change, 10 per issue — [Cleric pricing](https://cleric.ai/pricing), Oct 2026. Azure's 1,300+ internal agents is the largest disclosed deployment ([Microsoft](https://techcommunity.microsoft.com/blog/appsonazureblog/announcing-general-availability-for-the-azure-sre-agent/4500682), 2026). Other logos are vendor-selected, not adoption rates.

### Inferences
- Run a **shadow-mode pilot**: the agent observes real incidents and produces diagnoses but cannot act; compare its RCA to the human RCA over 20–50 incidents. This measures precision without risk and matches what practitioners say they want.
- Willingness to grant write access tracks **reversibility of the action**, not trust in the model. Teams will let an agent restart a pod; almost none will let it run a migration. Vendor "70–80% RCA accuracy" claims need denominator, incident mix, and adjudication disclosed; ITBench-AA's 56% on static diagnosis is a better prior.

### Gaps
- No independent survey of AI SRE adoption or satisfaction with published methodology (Gartner/Forrester are paywalled).
- No disclosed production incident caused by an AI remediation action; absence of evidence likely reflects limited write access, not proven safety.
