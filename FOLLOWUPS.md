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

## 6. Arm A is no longer 1.00 on RCA by construction

The docs, DEMO.md and the arm A tolerance band (`rca_accuracy:
Band(1.0, 1.0)`) all say arm A scores 1.00 because every catalogue
signature has a rule. Phase 2's `xsys-runtime-missing-owner` deliberately
breaks that: its signature has no arm-A rule by design, so arm A answers
"no deterministic rule matched this signal signature", escalates (the
expected final state), and measures correctly - but scores an RCA miss.
On the 19-scenario catalogue arm A is 13/14 on RCA, deterministically.

Not a contamination signal: it is identical on every run. Decide between
(a) keeping abstention as a miss and moving arm A's band and the "1.00"
wording to 13/14 with spread 0, or (b) scoring a rule-table abstention as
not-measured. (a) is the conservative reading - an abstention found no
root cause. Do not widen the band just to make a run pass. Found on the
first full 19-scenario run, 2026-10-02.

## 7. Helix does not yet run on its own

Found 2026-10-02 while scoping live Jira/Slack. Everything Helix does
today starts with someone triggering it. For hands-off operation:

- **Jenkins -> Helix exists but stops at intake.** The Jenkinsfile's
  `post { failure }` posts to `/intake/jenkins` without `?run=true`, so an
  incident is recorded and never driven.
- **Nothing watches Kubernetes.** D6 says "no always-on watcher exists in
  Phase 1 (that is E3 territory)", but E3's adapters are pure functions
  over already-fetched payloads (D18), and no fetch/watch loop was ever
  built. A crashing pod only reaches Helix via `scripts/emit-signal.sh`.
- **No GitHub webhook endpoint.** `events/adapters/github.py` exists, but
  nothing receives GitHub's webhooks. A local Helix also needs a public URL
  (a tunnel) for GitHub to reach it.
- **`helix consume` records, it does not drive.** `run_consumer` ingests
  and correlates events into incidents but never calls `run_incident`.

Live Jira/Slack/Notion also need real credentials in `.env`. Change `.env`
only when no benchmark is running: every `helix` subprocess re-reads it, so
switching `HELIX_WORK_PROVIDER` mid-run would send benchmark incidents to
real Jira.

## 8. A provider rate limit is scored as the arm failing

The first five-arm run (`benchmark/reports/phase2-run1.json`,
2026-10-02) exhausted Groq's daily token budget during arm E: its last
five scenarios ended `failed` with "diagnosis produced no hypothesis:
... Error code: 429 ... tokens per day (TPD)". The harness scored them as
arm E failures. A 429 is the provider refusing, not the arm reasoning
badly, and should be recorded like a missing key is - skipped with the
reason, never entering a rate. Until then, recompute arm E without those
five rows (done by hand in the run's analysis).

## 9. Owner and reviewer accuracy differ by arm

In the same run, arms A and E scored owner 1.00 / reviewer 1.00 while B,
C and D all scored 0.83 / 0.75. Triage and review are deterministic and
should not depend on the arm, so either a change scenario takes a
different path through B/C/D (the fused node has no replan, so one may
end before review), or something arm-specific leaks into triage.
Unexplained; check before trusting the Phase 2 lifecycle columns.

## 10. Arm D ran without its graph

`HELIX_GRAPH_ENABLED` defaults to false and Neo4j is not set up, so in
phase2-run1 arm D received retrieval plus the event timeline but an
empty graph neighbourhood. The run measures "timeline context" (it made
diagnosis worse: RCA 0.78 for C, 0.22 for D on the same 13 scenarios),
not graph context. 2.7's with-and-without-graph comparison is still
unmeasured.

## 11. Jira declares a "blocked" state that Kanban projects lack

`JiraProvider.capabilities()` is static and always lists `blocked`, but a
team-managed Kanban project's workflow is only To Do / In Progress / Done.
Live, `transition_work_item(..., "blocked")` raised a clean WorkItemError
("jira workflow offers no transition for 'blocked'"), so nothing breaks -
an escalated incident's item just stays in its previous column. Per
plan.md 2.10, an undeclared gap is a defect in the abstraction: either
read the project's workflow (`/rest/api/3/project/{key}/statuses`) into
the capability map, or add a Blocked column to the project.

## 12. Slack message edits need a channel id, not a name

Live test 2026-10-03: `WebSlackClient.update_message("#helix-incidents",
...)` fails with `channel_not_found`. Slack's chat.postMessage accepts a
channel name, but chat.update only accepts the channel id (`C...`), which
every postMessage response returns. Dormant: no production path calls
`update_message` today (the outbox only posts, threading updates under
the first message). If edits are ever used - e.g. replacing approval
buttons after a decision - store the `channel` id from the post response
alongside `ts` and pass that.

## 13. rollback_deployment can roll back onto a broken revision

Live 2026-10-03 (INC-6b5c0922): arm E's rollback ran `rollout undo`, which
goes to the previous ReplicaSet revision, and on the sandbox that was an
earlier injected fault - the new pod crashlooped. Rollback assumes the
previous revision was healthy, which holds for a normal release history
and fails for one full of benchmark faults. The per-domain coherence check
now keeps a CI failure from reaching rollback at all, and verification
caught the result (pods_ready=fail -> escalated), so nothing stayed
broken unnoticed. A stronger fix would roll back to the last revision
Helix verified healthy rather than to whatever came before.

## 14. A fix PR marks a runtime incident recovered while it is still broken

Found 2026-10-03 in the first fully unattended run (INC-aba102e5). `helix
watch` caught an injected crashloop, `helix consume --run` drove it, and
arm E chose open_fix_pr. Verification checked fix_pr_exists=pass, the
incident went to `recovered`, Jira KAN-116 closed as Done - and the pod was
still in CrashLoopBackOff. A PR changes nothing until it is merged and
deployed.

Unattended, this becomes a silent failure. The watcher reports a condition
once per occurrence, and ingest never attaches to a terminal incident, so
the still-crashing pod produced nothing further. Nobody would know.

open_fix_pr's verification proves the PR exists, which is the right check
for a CI or code incident, where the PR is the remediation. For runtime
evidence (pod.failed, service.unhealthy, deployment.failed) it is not
recovery. Options: (a) a runtime incident remediated by a PR parks in a
non-terminal "fix proposed" state until the runtime check passes; (b)
verification for runtime incidents always includes the runtime legs
(pods_ready, rollout_complete) whatever the action; (c) the watcher
re-publishes a condition that persists past a deadline after being
reported. (b) is closest to plan.md 1.9's intent; (c) alone would just
open new incidents that close the same way.
