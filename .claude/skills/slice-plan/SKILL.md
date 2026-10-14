---
name: slice-plan
description: Plan remaining work as thin, shippable slices, get the user's approval, then build it slice by slice.
disable-model-invocation: true
---

Turn a pile of remaining work into an approved plan of **slices**, then
build it. A slice is one finding or feature taken end to end: reproduced,
fixed or built, tested, committed on its own. The user approves the plan
once; nothing is built before that.

## 1. Gather

Collect every candidate item from what the user named, or, when they named
nothing, from `FOLLOWUPS.md`, `plan.md` open commitments, the newest
`HANDOFF-*.md`, and `future_plans.md`. Drop what is closed or deferred by
the user's decision, and say which you dropped and why.

Done when every candidate is listed with its source.

## 2. Investigate

For each candidate, read the code it touches and reproduce it where it is a
bug (a failing test, a script against SQLite or a throwaway database). Find
out what it really takes before sizing it. A candidate that turns out to be
already fixed, not a bug, or blocked on something only the user can supply
leaves the slice list here, with the reason.

Done when every remaining candidate has a confirmed cause or scope, the
files it touches, and a size (small under 30 min, medium 30-90 min, large
more).

## 3. Slice

Write one slice per item. Each slice states:

- **What** in one plain sentence, and **why it matters**.
- **Proof first**: the test or reproduction that fails before the change.
- **Change**: files and approach, smallest correct one.
- **Done when**: a checkable criterion (test green, benchmark check passes,
  live behaviour observed).
- **Risk**: what it could break, and what verifies it did not.
- **Size**.

Order slices so each one leaves `main` working. Mark any slice that needs a
user decision (a design choice, a credential, a cost, a long run) and write
the decision as a question with a recommended answer. Add the total time.

Done when every slice has all six parts and every decision is a question.

## 4. Ask

Show the plan in chat as a short table (slice, what, size, decision
needed), then ask with `AskUserQuestion`: one question per open decision,
recommended option first, plus a final question to approve the plan as is,
approve with the slices the user drops, or change it. Revise and ask again
until the user approves.

Done when the user has approved a specific list of slices.

## 5. Build

Work the approved slices in order. For each one:

1. Write the proof and watch it fail.
2. Make the change; watch the proof pass.
3. Run lint and the full suite; check the real exit codes.
4. Commit that slice alone, message saying what was wrong and why the fix.
5. Update its `FOLLOWUPS.md` or audit entry with the commit.

Stop and ask when a slice turns out larger, riskier or different from the
plan; carry on with the next slice only once the user says so. Subagent
output is a first draft: re-check it against the slice's proof before
committing.

Done when every approved slice is committed or explicitly handed back with
a reason.

## 6. Report

A table of slices with commit, result and anything that changed from the
plan, then what is left and what only the user can do (push, PR, a
decision). Keep it plain English.
