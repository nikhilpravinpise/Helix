# Demo runbook

A 6 minute live demo of Helix recovering a real CI failure and opening a
real pull request. Everything below has been run end to end.

## Before you start

```bash
make db-up && make db-migrate     # Postgres, ~10s
uv run helix serve                # operator UI on http://127.0.0.1:8787/ui/
```

Leave `helix serve` running in its own terminal. Open two browser tabs:

1. **http://127.0.0.1:8787/ui/** - the operator UI
2. **https://github.com/VrajVed/helix-sandbox/pulls** - where the PRs land

Close PR #1 first if it is still open. It is empty (0 files changed) from
the first run, before the sandbox repo had the bug in it, and an empty PR
on screen invites exactly the question you do not want mid-demo.

## The setup line

> "Helix watches engineering systems. When something breaks, it works out
> why, decides what to do, checks the action is safe, does it, and then
> verifies it actually worked. It never merges its own code."

## Beat 1 - show the bug (30s)

Open `sandbox/payment-api/tests/test_main.py` in the sandbox repo on
GitHub, line 58:

```python
assert response.status_code == 999
```

> "A developer committed a broken assertion. CI fails on every run."

## Beat 2 - the signal arrives (30s)

```bash
cat /tmp/.../signal.json   # or just show it in the editor
```

```json
{"source": "jenkins", "service": "payment-api",
 "environment": "sandbox", "severity": "high",
 "signature": {"job": "payment-api", "stage": "Test"}}
```

> "This is all Helix gets. A job name and a failed stage. It does not get
> told what is wrong or what to do."

## Beat 3 - run it (90s)

```bash
HELIX_ARM=A HELIX_EVIDENCE_SOURCE=live uv run helix intake --file signal.json --run
```

Output:

```
helix intake: created incident INC-xxxxxxxx
helix intake --run: status=recovered run=terminal
```

While it runs, narrate the loop: **diagnose -> plan -> validate ->
policy gate -> execute -> verify.**

## Beat 4 - the pull request (90s) - the money shot

Refresh the GitHub tab. A new PR is there:

**"Helix remediation for INC-xxxxxxxx"**, branch `helix/INC-xxxxxxxx`,
1 file changed, +1 -3.

Open the diff. Helix removed the broken assertion and restored
`== 200`.

Three things to say here, in order:

- **"It opened the PR. It did not merge it."** A human still reviews.
  That is a deliberate safety boundary, not a missing feature.
- **"It can only write inside `sandbox/payment-api/`."** Enforced in
  code (`integrations/github/write.py`), and it refuses any path whose
  parent directory does not exist in the repo - which is how we caught a
  model inventing a Java source tree in a Python project.
- **"High-risk actions never execute unattended."** They park at
  `awaiting_approval` and wait for a human.

## Beat 5 - the evidence chain (90s)

Switch to the operator UI, click the incident. Show the provenance:
the signal, the evidence gathered, the hypothesis with its confidence,
the candidate actions with the chosen one, the policy verdict, the
execution row, and the verification result.

> "Every claim Helix makes is traceable to the evidence that produced
> it. Nothing here is the model asserting something unbacked."

## Beat 6 - the research result (60s)

This is the part that makes it a project rather than a script. Show
`benchmark/reports/latest.md`:

| arm | what it is | RCA accuracy |
|---|---|---|
| A | deterministic rules, no LLM | 1.00 |
| C | LLM + retrieval | 0.83 |
| B | single LLM call | 0.33 |
| E | full agent graph | 0.17 |

> "We measured whether more architecture actually helps. Retrieval
> earned its place - arm C beats a bare LLM on every reasoning metric.
> The full graph did not; it is the worst LLM arm and the least stable.
> That is a negative result and we are reporting it, because the whole
> point of the ablation was to find out rather than assume."

Arm A reading 1.00 is the control, not a win - it is a lookup table over
known fault signatures. Say so before someone asks.

## If it fails live

Run it once beforehand and keep that incident open in a tab. The
operator UI shows any prior incident's full chain, so you can present
Beats 4-6 from a completed run without re-running anything.

## Questions you will get

**"Did it actually fix the bug or just pattern-match?"**
On arm A, it matched a known fault signature to a known action - that is
what arm A *is*, and it is the control. The LLM arms reason from
evidence, and their numbers are in the ablation table, including where
they lost.

**"What stops it doing something destructive?"**
Three layers: a policy gate that checks the action's risk tier and
whether it is coherent with the evidence (a rejected PR cannot authorise
a cluster rollback), a write path restricted to one directory, and an
approval gate for anything high-risk. Plus it never merges.

**"Is this just calling an LLM?"**
Arm A uses no LLM at all and scores best. That is in the results.

## Reset between runs

Each run creates a new incident and a new PR, so you can demo repeatedly.
To start clean:

```bash
# close the open PRs in the sandbox repo, then re-seed the bug:
cd /tmp/hs && git revert --no-edit <the fix commit> && git push
```
