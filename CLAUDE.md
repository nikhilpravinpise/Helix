# CLAUDE.md

Behavioral guidelines for working in the Helix repository. These reduce common LLM coding mistakes and encode Helix-specific engineering rules from [CONTEXT.md](CONTEXT.md). Read CONTEXT.md for the full architecture and phase plan before doing non-trivial work.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

---

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

- Never guess at unstated requirements, schemas, integration behavior, or intent. If something isn't pinned down in CONTEXT.md or the request, **ask a clarifying question instead of assuming an answer.**
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.
- This applies especially to Phase boundaries (do not pull Phase 2 architecture into Phase 1 work) and to any decision with a safety or production-risk implication - when in doubt, ask rather than proceed.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.
- Do not add integrations, agents, or infrastructure (Kafka, Neo4j, MCP, a new provider) just because a later phase will eventually want them. Add abstractions only when a concrete, present need forces them.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## 5. Tests Are the Source of Truth

**A failing test means the code is wrong - not the test.**

- When a test fails, the default assumption is that the implementation has the bug. Fix the code.
- Never edit, weaken, skip, or delete a test just to make it pass. Do not change expected values, loosen assertions, add `xfail`/`skip` markers, or narrow test scope to dodge a real failure.
- The only valid reasons to change a test are: the test itself has a bug (wrong setup, wrong expected value for reasons unrelated to the code under test), or the requirements genuinely changed and the user confirmed the new expected behavior. State explicitly which of these applies before touching a test - if neither clearly applies, fix the code instead and say so.
- Tests must cover edge cases, not just the happy path: empty/null inputs, boundary values, failure and timeout paths, concurrent/duplicate events, and the unhappy-path state transitions described in CONTEXT.md (retries, escalation, rejected validation, replanning).
- Reproduce bugs end-to-end before fixing them, as close to the real user/system experience as possible (e.g. an actual CI failure or K8s event, not a mocked shortcut), then write the regression test from that reproduction.
- Treat lint failures, test failures, and flaky tests as defects to be fixed, not noise to be suppressed.

---

## 6. Helix-Specific Engineering Principles

(from [CONTEXT.md §22](CONTEXT.md))

- Phase 1 must be useful without Phase 2. Phase 2 must extend Phase 1, never replace it.
- Add abstractions only when a repeated, concrete need appears - not for hypothetical future integrations.
- Do not add integrations for visual architecture alone.
- Prefer GitHub PRs for persistent code/configuration changes over direct writes.
- Use least privilege. High-risk production actions require human approval.
- Never expose arbitrary shell access to the LLM.
- Do not make vendor APIs (GitHub, Jenkins, Jira, etc.) part of agent reasoning - go through capability interfaces.
- Do not add Neo4j or GraphRAG in Phase 1.
- Do not add MCP or a large tool-control layer before a concrete reuse or decoupling need exists.
- Prefer a small number of reliable agents over a large swarm.

## 7. Global Agent Instructions

(from [CONTEXT.md §23](CONTEXT.md))

- Never use the em dash character. Use plain dash "-" instead.
- When writing commit messages, never auto-add your agent name as co-author.
- Never manually modify CHANGELOG.md files or any files marked as auto-generated.
- When making technical decisions, do not give much weight to development cost. Prefer quality, simplicity, robustness, scalability, and long-term maintainability.
- For one-off or infrequent operational work, start with the simplest direct end-to-end path. Do not build wrappers, control planes, policy layers, custom verifiers, or automation unless the direct path exposes a concrete blocker or repeated need that justifies the added machinery.
- When end-to-end testing a product, be picky about the UI and aim for pixel perfection. Fix obvious visual defects along the way.
- Before using "dynamic workflows", "ultra code", or any harness feature that immediately spawns a large swarm of subagents, explain the tradeoffs and ask for explicit approval.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, clarifying questions come before implementation rather than after mistakes, and no test is ever edited just to make bad code pass.
