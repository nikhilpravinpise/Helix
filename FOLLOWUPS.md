# Follow-ups

Known issues found but deliberately not fixed yet, with enough detail to
pick up cold. Nothing here blocks the Phase 2 measurements.

## 1. Any single-scenario benchmark run clobbers `latest.*`

`benchmark/harness.py:write_report` always writes
`benchmark/reports/latest.json` and `latest.md`, and those two files are
committed as "the current-evidence pointer" (see the directory's
`.gitignore`). So running one scenario for a quick check silently
replaces the committed multi-arm evidence with a one-scenario report.

Hit this while verifying arm A on `xsys-pr-deploy-failure`; recovered
with `git checkout benchmark/reports/latest.*`.

Options: only write `latest.*` when the run covers every arm requested,
or gate it behind an explicit `--promote` flag. Either way the
timestamped intermediate is already written, so nothing is lost.

## 2. Arm A's rule table does not cover the 7 Phase 2 fault names

`agent/rules.py` `RULES` has 12 entries, one per original Phase 1 fault
signature. The `org-*` and `xsys-*` scenarios declare faults like
`missing-owner-change` and `stale-work-item` that have no entry.

**This is probably fine and was checked once.** `benchmark/scoring.py:346`
scores `rca = None` for a change-flow run that never reached diagnosis,
rather than a fabricated miss, and `tolerance._series` skips `None`. So
those scenarios contribute nothing to `rca_accuracy` and cannot breach
arm A's `1.0` band.

The one change scenario that *does* reach diagnosis,
`xsys-pr-deploy-failure`, declares `fault: docker-build-failure` - an
existing Phase 1 fault - and was run directly on arm A during this pass:
it recovered, scoring 1.00 on rca, action validity, recovery, measured
state, owner, reviewer and lifecycle consistency.

Worth re-checking when the full five-arm ablation runs: if arm A reads
anything other than 1.00, this is the first place to look.

## 3. The Phase 2 metrics have no tolerance bands

`owner_accuracy`, `reviewer_accuracy`, `lifecycle_consistency`,
`duplicate_work_item_rate` and `decision_evidence_coverage` are scored
(`benchmark/scoring.py:421-434`) but appear in no `BANDS` entry, so the
three-run stability check does not cover any of them.

Deliberate: a band invented before a metric has ever been measured is a
guess dressed as a measurement. Fill them in after the first real
five-arm run, not before. Recorded in `docs/benchmark/tolerance.md`.

## 4. D14a sorts before D14 in plan.md

Cosmetic. `plan.md:154` is D14a, `plan.md:156` is D14. Left alone rather
than renumbering, since decision numbers are referenced from other
documents.

## 5. The incident page dumps fix-PR file contents as raw JSON

On `/ui/incidents/<id>` for an incident that ran `open_fix_pr`, the
selected candidate's params and the execution output each render the
full corrected file as one JSON string with literal `\n` escapes - a
3.8KB wall of text, shown twice. Nothing is wrong or missing, it is just
unreadable. Rendering `files[].content` in a `<pre>` block (or showing
path plus byte count and linking to the PR diff) would fix it. Found
while testing `./start.sh --run` on 2026-09-30.
