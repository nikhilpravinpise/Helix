# Independent Evidence on AI Code Review (as of October 2026)

Scope: pre-merge PR review only; AI incident response is excluded. Evidence is graded as **academic**, **independent benchmark**, **vendor-commissioned**, or **vendor claim**. A recurring trap: "acceptance/precision" measures whether developers acted, not whether the comment was correct; "defect detection" measures recall against a gold set, not production escape. Designs are not comparable.

## 1. How well does LLM-based PR review work — defects, false positives, time, trust, rubber-stamping?

### Takeaway
LLM review produces some useful comments but is noisy: independent field studies put acceptance of CodeRabbit/GitHub-Action comments at roughly 16–36%, and a 2026 MSR study finds CRA-only PRs merge 23 points less often than human-only PRs. Industrial deployments show modest real savings (Google: 7.5% of reviewer comments auto-resolved), and a randomized safety trial found AI patches *slowed* reviewers by >5% until the UX changed. No rigorous code-review field experiment shows rubber-stamping; the documented failure mode is noise-driven rejection and abandonment, not blind approval.

### Cited Findings
- **CodeRabbit field study (arXiv, 3 Jul 2026; v2 23 Jul 2026):** 31,073 comment/feedback pairs from 10,191 PRs across 239 GitHub repos and 10 languages. 36.4% accepted, 7.3% triggered discussion, **56.3% rejected**; rejections were mainly invalid suggestions (false positives, redundant, out of scope) and misalignment with intent. Functional-defect comments were 43.3% of all comments and rejected at 56.2%. Method: 10% stratified manual annotation, remainder GPT-5.1 annotation; only comments with an explicit developer reply. — [arXiv:2607.03316](https://arxiv.org/abs/2607.03316)
- **Human–AI synergy study (arXiv, Mar 2026):** 278,790 review conversations across 300 OSS projects. AI agents produced 88,011 suggestions vs 25,673 from humans, but adoption was **16.6% for AI vs 56.5% for human** suggestions; adopted AI suggestions increased code size/complexity more. — [arXiv:2603.15911](https://arxiv.org/abs/2603.15911)
- **GitHub Actions study (arXiv, Aug 2025):** 22,326 comments in 178 mature repos, 16 review actions. The best tool (`coderabbitai/ai-pr-reviewer`, the open-source Action, not the CodeRabbit SaaS) had a **19.2% valid-and-addressed rate** vs ~60% for humans; some tools were near zero. — [arXiv:2508.18771](https://arxiv.org/abs/2508.18771)
- **CRA-only PR outcomes (MSR 2026, 3 Apr 2026):** 3,109 AIDev PRs. CRA-only PRs merge at **45.20% vs 68.37%** for human-only; 60.2% of closed CRA-only PRs had 0–30% signal; **12 of 13 CRAs averaged below 60% signal**. Conclusion: augment, don't replace. — [arXiv:2604.03196](https://arxiv.org/abs/2604.03196)
- **Google AutoCommenter (AIware '24, May 2024):** tens of thousands of developers; estimated **~40% comment-resolution**, >80% useful ratio after suppression, 68% coverage of rules humans cite, but far less diverse (top 85 rules = 90% of comments vs 35% of human comments). Google-authored. — [arXiv:2405.13565](https://arxiv.org/abs/2405.13565)
- **Google ML edit resolution (ICSE-SEIP 2024):** authors applied an ML-suggested edit to **7.5% of all reviewer comments**; ~60 minutes of active shepherding per change. The "hundreds of thousands of engineer-hours" saving is an extrapolation. — [Google Research](https://research.google/pubs/resolving-code-review-comments-with-machine-learning/)
- **Meta MetaMateCR (arXiv, 17 Jul 2025):** 64k comment/patch pairs; exact-match patch 68% offline, 19.7% actionable-to-applied in production. A randomized safety trial found reviewers took **>5% longer** when AI patches were shown to reviewers; the regression disappeared only when patches were shown to authors. — [arXiv:2507.13499](https://arxiv.org/abs/2507.13499)
- **LinkedIn (company-reported, Aug 2026):** 63.9% combined acceptance on 1,727 PRs / 5,230 comments; security suggestions only 40.6%. Self-reported. — [Lumiary case summary](https://lumiary.ai/cases/linkedin-multi-agent-code-review)
- **WirelessCar field study (ESEM 2025):** developers preferred AI-led review for large/unfamiliar PRs, but trust varied with familiarity and risk; false positives were a concern. — [arXiv:2505.16339](https://arxiv.org/abs/2505.16339)
- **Rubber-stamping:** no code-review field experiment found. Adjacent automation-bias evidence is strong in medicine/aviation (210-participant study, 2024), but not transferable without testing. The closest causal code-review evidence is Meta's trial, which found *extra* verification time. — [PubMed 39234734](https://pubmed.ncbi.nlm.nih.gov/39234734/); [arXiv:2507.13499](https://arxiv.org/abs/2507.13499)

### Inferences
- A realistic planning assumption is that **half or more of AI review comments are rejected**. Functional/logic comments are not more accepted than style/evolvability comments in the CodeRabbit data.
- "Acceptance" is a lower bound on usefulness and an upper bound on correctness: developers sometimes accept wrong suggestions and often ignore correct ones. The 95%+ precision figures in vendor tests measure something else (see §3).
- The economic case is strongest for cheap, high-volume, low-risk comments and weakest for security/architecture, where both acceptance and evidence are lower.
- Rubber-stamping is plausible but **not the documented failure mode today**; low trust and noise fatigue are better supported. As AI-authored code rises, the risk may shift from "reviewers trust the bot" to "reviewers stop reading anything."

### Gaps
- No independent RCT measures end-to-end **review time or defect escape rate** for CodeRabbit in a real team.
- No study isolates whether humans approve *defective code* more often after an AI reviewer comments "LGTM" — the rubber-stamp hypothesis is untested in code review.
- Field studies use public repos and comments that got replies; private-enterprise behavior and silently ignored comments are under-sampled.

## 2. Do AI reviewers find security vulnerabilities as well as SAST/SCA/secrets tools — substitute or complement?

### Takeaway
The evidence supports **complement, not substitution**. Project-scale evaluation finds LLM detectors have higher recall on some semantic bugs but high false-discovery rates, incomplete API modeling, and very high cost; SAST remains cheaper and more deterministic but misses whole classes of logic/authz bugs and has its own false positives. No independent study shows an LLM PR reviewer matching Semgrep/CodeQL/Snyk/SonarQube on CWE coverage, dependency vulnerabilities, or secrets. CodeRabbit-specific security efficacy is essentially unmeasured independently.

### Cited Findings
- **Project-scale LLM vs SAST (arXiv, 27 Jan 2026; rev 25 Sep 2026):** 5 specialized LLM detectors, 2 general agents and 4 traditional analyzers; 265 known C/C++ and Java vulnerabilities across 24 projects; 6,442 warnings analysed. General agents had the highest recall but struggled with complex code; both LLM and traditional methods suffered incomplete API modeling; many tools had high false-discovery rates. LLM methods cost hundreds of thousands to hundreds of millions of tokens, **>$1,000 per project/module**, and hours-to-days runtime. — [arXiv:2601.19239](https://arxiv.org/abs/2601.19239)
- **Same study, Java recall:** average recall was **14.19% for CodeQL, 6.67% for Semgrep, 14.84% for SpotBugs**, versus higher recall for some LLM agents — but at far higher cost and with more false positives. — [arXiv:2601.19239](https://arxiv.org/abs/2601.19239)
- **Static tools vs human ground truth (arXiv, Feb 2026):** 1,080 human-validated LLM-generated samples; 61% were genuinely secure, while Semgrep classified 60% secure and CodeQL 80%. Per-sample agreement was only **65% (Semgrep) and 61% (CodeQL)**; CodeQL recall 0.34 / precision 0.67, Semgrep 0.54 / 0.52 on target CWEs. — [arXiv:2602.05868](https://arxiv.org/pdf/2602.05868v1)
- **Formal-verification stress test (arXiv, Apr 2026):** of 90 Z3-proven vulnerabilities, all industry tools combined caught 2; **97.8% were invisible to Semgrep, CodeQL, Cppcheck, Clang SA and FlawFinder**; CodeQL caught 0/90. Shows tool blind spots, but on an adversarial AI-generated corpus, not production code. — [arXiv:2604.05292](https://arxiv.org/pdf/2604.05292)
- **Semgrep vendor claim (2026):** an LLM-only IDOR baseline had **88% false positives**; Semgrep's static-plus-LLM workflow produced 8× more true positives and 50% fewer false positives. Vendor-run, no sample size or independent replication disclosed. — [Semgrep](https://semgrep.dev/blog/2026/introducing-semgrep-custom-workflows)
- **Secrets and SCA:** no independent study found comparing LLM PR reviewers with dedicated secret scanners (GitHub secret scanning, TruffleHog, Gitleaks) or SCA tools (Snyk, Dependabot). CodeRabbit bundles 40+ linters/scanners but publishes no comparative detection rates. — gap, no reliable source.

### Inferences
- LLM reviewers are best treated as an **additional semantic layer** for authz/IDOR/business-logic and cross-file issues that pattern tools miss; SAST/SCA/secrets scanners remain the deterministic, auditable control for known CWEs, dependencies and credentials.
- Replacing SAST with an LLM reviewer would reduce repeatability and increase cost per scan by orders of magnitude, with no evidence of equal coverage.
- If a team already runs Semgrep/CodeQL/Snyk, the marginal value of an LLM reviewer is in *different* bug classes and triage — not replacement.

### Gaps
- No head-to-head of CodeRabbit vs Semgrep/CodeQL/Snyk/SonarQube on the same labelled vulnerability set.
- No rigorous evidence on LLM **secrets detection**; secret scanning is largely deterministic and LLM evidence is absent.
- No independent evidence on LLM review of **dependency vulnerabilities** (SCA), where Snyk/Dependabot have database-driven advantages.
- Vendor security benchmarks rarely disclose corpus construction, inter-rater agreement, or prompt tuning.

## 3. What independent evidence exists specifically for CodeRabbit?

### Takeaway
There is one substantial academic field study of CodeRabbit (31,073 comments, 239 repos, 56.3% rejection), two benchmark evaluations with **wildly conflicting precision numbers** (49.2% vs 95.88%) because they measure different things, and one severe independent security audit (RCE, ~1M repos exposed). No public independent audit of CodeRabbit's *review quality* exists; Signal65 is a commissioned testing firm, and Martian is independent but not peer-reviewed.

### Cited Findings
- **Academic field study (Jul 2026):** the strongest independent quality evidence — 31,073 comments, 239 repos, 56.3% rejected (details §1). Limits: only comments with explicit replies; 10% manual + GPT-5.1 annotation; no ground-truth check; university authors, no vendor funding disclosed on the paper page. — [arXiv:2607.03316](https://arxiv.org/abs/2607.03316)
- **Martian Code Review Bench v0 (26 Feb 2026):** open-source methodology, ~1M PRs, 10 tools. Initial: CodeRabbit **#1, F1 51.2%, precision 49.2%, recall 53.5%, 284,696 PRs**; Greptile #2 (50.6% F1). Martian says it is "funded independently of the code review market." — [Martian](https://withmartian.com/post/code-review-bench-v0); [leaderboard](https://codereview.withmartian.com/)
- **Martian leaderboard as of Sep 2026:** methodology/refresh changed the ranking: CodeRabbit **#3, F1 62.3%, precision 72.6%, recall 54.5%, 362,166 PRs**, behind Cubic Dev AI and Greptile (both 63.3% F1). Weekly view had Greptile #1. "CodeRabbit #1" claims are time- and method-sensitive. — [Martian leaderboard](https://codereview.withmartian.com/)
- **Signal65 study (30 Mar 2026):** 5 tools, 6 OSS repos, **60 replayed historical bugs**, manual grading. CodeRabbit: **95.88% precision, 93 true positives, 4 false positives, 25 critical bugs** (most of any tool); Copilot 64.35% precision with 41 false positives. The public summary reports no inter-rater reliability or confidence intervals. — [Signal65](https://signal65.com/research/evaluating-ai-code-review-tools-a-real-world-bug-detection-study)
- **Signal65 funding:** Signal65 sells commissioned testing; its page has a "Research commissioned by:" field (the logo did not render), and CodeRabbit is the only vendor benchmarked and promotes the result. Treat as **vendor-commissioned** until confirmed. — [Signal65 governance](https://pinnacle.signal65.com/benchmark-governance)
- **Security audit (Kudelski, disclosed 19 Aug 2025; vulnerability from Jan 2025):** a malicious PR using `.rubocop.yml` achieved **RCE on CodeRabbit production**, leaked Anthropic/OpenAI/LangChain/Pinecone keys, PostgreSQL credentials and the GitHub App private key, giving read/write access to **~1M repositories**. CodeRabbit remediated within hours, rotated secrets, sandboxed Rubocop; says no customer data was accessed. — [Kudelski](https://kudelskisecurity.com/research/how-we-exploited-coderabbit-from-a-simple-pr-to-rce-and-write-access-on-1m-repositories); [CodeRabbit response](https://www.coderabbit.ai/blog/our-response-to-the-january-2025-kudelski-security-vulnerability-disclosure-action-and-continuous-improvement)
- **Compliance posture:** SOC 2 Type II (1 Mar 2025–28 Feb 2026), ISO 27001:2022 and a GDPR audit are listed, but reports and the pentest assessment are **gated**, not public. — [CodeRabbit Trust Center](https://trust.coderabbit.ai/resources)

### Inferences
- CodeRabbit's preferred statistic (49.2% precision = "one in two comments leads to a code change") is **adoption, not correctness**, and comes from a benchmark that later ranked it third. The 95.88% Signal65 figure measures manual correctness on 60 seeded bugs. Both can be true; neither is a production defect-escape rate.
- The academic 56.3% rejection rate is the most defensible single quality signal: large, public-repo based, not vendor-funded — but it still measures developer reaction, not ground truth.
- The security incident is a **platform-risk** finding, not evidence that review comments are wrong. It argues for treating any reviewer with repo write access as a supply-chain dependency.

### Gaps
- No independent, public **quality audit** of CodeRabbit on a held-out corpus with inter-rater agreement.
- No peer-reviewed head-to-head including CodeRabbit and SAST/SCA tools.
- Signal65's funder is not confirmed from the extracted page; cite as vendor-commissioned.
- No data on enterprise/private-repo performance; all large studies use public GitHub.

## 4. What do public "AI review benchmark" claims actually measure, and what are their limits?

### Takeaway
Most public benchmarks measure either **developer adoption** (did a comment precede a code change?) or **recall against a small replayed/human-labelled bug set** — neither measures production defect escape. They are small, vendor-authored or vendor-commissioned, drawn from public PRs, and unstable across methodology versions. Martian's own methodology page is the best public critique of the genre; c-CRAB's test-based design is the most promising correction, and it shows agents solving only ~40% of tasks humans identified.

### Cited Findings
- **Martian's critique (2026):** existing benchmarks are "small (50–100 PRs), vendor-published (the vendor's tool wins), unrefreshable (stale data, training contamination), and under-defined (no shared notion of what counts as a bug or how to measure precision and recall)." — [Martian methodology](https://github.com/withmartian/code-review-benchmark/blob/main/methodology/summary.md)
- **What Martian measures:** precision = a bot suggestion preceded a real code change, analysed by LLM; recall = match against a gold set, with filters for user preferences. It acknowledges "not acted on" ≠ "wrong" and that a comment matching `agents.md` can be a true positive even if ignored. — [Martian methodology](https://github.com/withmartian/code-review-benchmark/blob/main/methodology/full.md)
- **Instability:** CodeRabbit went from #1 (F1 51.2%, precision 49.2%) in Feb–Mar 2026 to #3 (F1 62.3%, precision 72.6%) by Sep 2026 on the same leaderboard. A March vendor blog citing "highest F1" was stale within six months. — [CodeRabbit blog, 3 Mar 2026](https://www.coderabbit.ai/blog/coderabbit-tops-martian-code-review-benchmark); [current leaderboard](https://codereview.withmartian.com/)
- **c-CRAB (arXiv, 24 Mar 2026):** converts human review comments into executable tests, avoiding LLM-judge circularity. Agents collectively solve **~40% of tasks**; Claude Code 32.1% pass, Devin 24.8%, PR-Agent 23.1%, Codex 20.1% (human reviews = 100% by construction). — [arXiv:2603.23448](https://arxiv.org/abs/2603.23448)
- **GitHub ReviewBench (5 Oct 2026):** vendor-authored, modelled on 103.9M real PRs, with an offline set and online A/B validation (addressed rate +8.0%, recall +13.6%, comment volume +61%, cost/review −8.0%). Good methodology, but GitHub measures its own Copilot; no third-party replication. — [GitHub Blog](https://github.blog/ai-and-ml/github-copilot/reviewbench-an-open-benchmark-for-ai-code-review/)
- **Vendor conflation:** CodeRabbit's March 2026 post equates 49.2% precision with "roughly one in two comments leads to a code change" and calls the benchmark "the largest and most credible public benchmark" — a self-interested framing of an adoption metric. — [CodeRabbit](https://www.coderabbit.ai/blog/coderabbit-tops-martian-code-review-benchmark)

### Inferences
- Benchmark claims are **relative, directional signals under a specific scoring definition**, not absolute quality. A tool can "win" by commenting more (higher recall, lower precision) or less (higher precision, lower recall); F1 hides which error a team cares about.
- **Contamination is largely unmeasured.** Public PRs used by Martian, c-CRAB and academic studies may be in training data; no study quantifies this for code review.
- **Cherry-picking** operates at three levels: repo selection (6 repos in Signal65), bug selection (60 replayed bugs), and bot-participation bias (Martian only sees PRs where a bot commented). None uses a pre-registered, randomly sampled production corpus.
- The most credible approach combines a **test-based benchmark** (c-CRAB), **online adoption/outcome data** (Martian/GitHub A/B) and an **independent academic field study** (arXiv:2607.03316). No single number should drive a purchase.

### Gaps
- No benchmark measures **defect escape rate** (production bugs a reviewer saw and missed) or **cost per true positive** in a real team.
- No independent measurement of **training-data contamination** for code-review benchmarks.
- No standard severity weighting: a missed critical authz bug and a missed naming nit count the same in most precision/recall calculations.
- Vendor-funded studies rarely publish raw annotations, inter-rater agreement, or prompts, making replication difficult.

---

**Bottom line:** independent evidence supports AI review as a *noisy complement* to human review and SAST, with typical comment acceptance around 16–36% and substantial rejection for false positives/out-of-scope comments. CodeRabbit has the best large-scale academic field evidence of any vendor (unflattering on rejection), a vendor-commissioned 95.88%-precision study that conflicts with adoption-based benchmarks, and a serious 2025 platform RCE. Claims that AI review replaces SAST/SCA/secrets scanning, or that benchmark rankings are stable, are not supported as of October 2026.
