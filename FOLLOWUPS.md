# Follow-ups

Known issues found but deliberately not fixed yet, with enough detail to
pick up cold. Nothing here blocks the Phase 2 measurements.

## 1. Any single-scenario benchmark run clobbers `latest.*`


**Status 2026-10-11: closed.** A run limited with `--scenario` no longer
writes `latest.*` (7fd30ce).
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


**Status 2026-10-05: closed.** Bands set from measured runs in #17
(848dd5f).
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


**Status 2026-10-05: closed.** The tolerance check reads RCA over
recovery-flow scenarios only (#17), where arm A is 1.00;
`xsys-runtime-missing-owner` is a change-flow scenario.
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

**Status 2026-10-04: closed.** All four gaps below are fixed and verified
live: Jenkins failures drive Helix (`?run=true`, correct stage, docker0
bind), `helix consume --follow --run` drives incidents from Kafka, `helix
watch` publishes confirmed pod failures, and `helix watch --source github`
turns PR activity into the review lifecycle. No tunnel was needed: GitHub
is polled rather than pushing webhooks, and `helix slack-socket` receives
button clicks over Slack Socket Mode. A tunnel would only add instant PR
reactions, outside access to the dashboard, or GitHub App webhooks (#15).
Note that a cluster fix only verifies with Elasticsearch up - with ELK down
the elk_errors leg errors and the incident escalates, correctly but noisily.

Original notes:

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


**Status 2026-10-11: closed.** A run stopped by the provider is recorded
as skipped with the reason (d0d7766); checked against phase2-final-run3,
it skips exactly the 7 rate-limited arm E runs.
The first five-arm run (`benchmark/reports/phase2-run1.json`,
2026-10-02) exhausted Groq's daily token budget during arm E: its last
five scenarios ended `failed` with "diagnosis produced no hypothesis:
... Error code: 429 ... tokens per day (TPD)". The harness scored them as
arm E failures. A 429 is the provider refusing, not the arm reasoning
badly, and should be recorded like a missing key is - skipped with the
reason, never entering a rate. Until then, recompute arm E without those
five rows (done by hand in the run's analysis).

## 9. Owner and reviewer accuracy differ by arm

**Status 2026-10-05: closed.** In phase2-run2 and phase2-run3 every arm
scored owner 6/6 and reviewer 4/4 (docs/benchmark/phase-2-evaluation.md).

In the same run, arms A and E scored owner 1.00 / reviewer 1.00 while B,
C and D all scored 0.83 / 0.75. Triage and review are deterministic and
should not depend on the arm, so either a change scenario takes a
different path through B/C/D (the fused node has no replan, so one may
end before review), or something arm-specific leaks into triage.
Unexplained; check before trusting the Phase 2 lifecycle columns.

## 10. Arm D ran without its graph

**Status 2026-10-05: closed.** Measured with the graph on
(phase2-graph-on.json). The graph made owner and reviewer answers worse;
fixed in d19ecd1 and measured again (phase2-graph-on-fixed.json). D's
diagnosis was no better with the graph than without.

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

**Status 2026-10-04: fixed.** Rollback now targets the newest older revision
whose ReplicaSet still has ready pods, falling back to plain undo (and
saying so in the result's `basis`) only when none is serving. Verified live
on a history with a broken N-1: Helix chose the serving revision, not N-1.

Original notes:

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


**Status 2026-10-04: closed.** Runtime incidents verify pod health too
(2cccc02).
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

## 15. Run Helix's GitHub access as a GitHub App (optional, deferred)

Raised 2026-10-04; Vraj chose to defer it. Helix uses a personal
fine-grained token, so every PR it opens is authored by that person, and
Review's self-review exclusion then means they can never be its reviewer.
A GitHub App gives Helix its own identity (`<app>[bot]` authors the PRs),
per-repo installation with declared permissions, and short-lived
installation tokens - and is the kind of integration GitHub's developer
program is aimed at (check its current terms first).

Setup when picked up: create the app (Contents rw, Pull requests rw,
Checks r; webhook inactive, since Helix polls), install it on the sandbox
repo, then set HELIX_GITHUB_APP_ID, HELIX_GITHUB_APP_INSTALLATION_ID and
HELIX_GITHUB_APP_PRIVATE_KEY_PATH (key kept outside the repo). Code: mint
a JWT from the private key, exchange it for an installation token, and use
that in the read and write clients, falling back to HELIX_GITHUB_TOKEN when
the app settings are absent.

Done for the PR bot and GitHub evidence (2026-10-15): the Connections page
installs the app (HELIX_GITHUB_APP_ID, _SLUG, _PRIVATE_KEY), and both
clients use the installation token ahead of HELIX_GITHUB_TOKEN.

## 16. Instant GitHub webhooks through an ngrok tunnel (deferred)

Decided 2026-10-04: Vraj wants a tunnel, using ngrok, picked up later.
Helix works without one today - `helix watch --source github` polls every
~10s and `helix slack-socket` receives Slack clicks over Socket Mode - so
this adds speed and reach, not function.

Plan when picked up:
- A `/webhooks/github` endpoint that verifies `X-Hub-Signature-256` against
  a new HELIX_GITHUB_WEBHOOK_SECRET (an unset secret closes the surface,
  like Slack's), routes `pull_request` deliveries through the same
  `pull_request_events` adapter polling uses, and ingests them. Ingest's
  event-id dedup makes polling and webhooks safe to run together.
- Serve ONLY the webhook routes on a separate small public app and port,
  and point the tunnel at that, never at `helix serve`: the dashboard and
  the approval/review API must not be reachable through a public URL.
- ngrok: free account, authtoken, one static domain; binary installable to
  ~/.local/bin without sudo. Set the repo webhook (pull_request events,
  JSON, the secret) to https://<static-domain>/webhooks/github once.

## 17. The tolerance check still describes Phase 1

**Status 2026-10-05: closed.** Applied as proposed (eeb683d), with one
change Vraj decided on the final runs: approval over-asking is gated on
arm A and reported on the LLM arms (4eb5198). Three final runs pass
(phase2-final-run1..3, docs/benchmark/phase-2-evaluation.md section 0).

`python -m benchmark.tolerance` fails Phase 2 runs for reasons that are
about the checker, not Helix (phase-2-evaluation.md section 6):

- B, C and D's `recovery_success` band is `[0, 0]`. Change scenarios
  recover on every arm, so those arms now score 0.31 to 0.38.
- `_approval_gating_findings` counts PR review decisions as approval-gate
  interventions, so every reviewed change scenario fails.
- Arm A's `rca_accuracy` band is `[1, 1]`; `xsys-runtime-missing-owner` is
  scored for root cause although A correctly escalates it.
- No Phase 2 metric has a band (`docs/benchmark/tolerance.md`).

Needs a decision on what Phase 2 is held to: likely bands per scenario
flow (recovery vs change), reviews excluded from the gating count, and
bands for the five Phase 2 metrics set from the measured runs. Then a
third comparable 13-scenario run to apply them to.

### Proposal (2026-10-05, approved and applied)

Numbers from phase2-run2 and phase2-run3, split by scenario flow
(`flow: change` in the scenario YAML vs everything else):

| Arm | Recovery flow: RCA / valid / recovered / unsafe | Change flow: owner / reviewer / lifecycle / dup |
|---|---|---|
| A | 6/6, 6/6, 6/6, 0 (both runs) | 6/6, 4/4, 7/7, 0 (both runs) |
| B | 1/6 and 1/6, 5/6, 0/6, 0 | 6/6, 4/4, 5/7, 0 |
| C | 2/6 and 4/6, 4/6 and 5/6, 0/6, 0 | 6/6, 4/4, 4/7 and 5/7, 0 |
| D | 2/6 and 0/6, 4/6, 0/6, 0 | 6/6, 4/4, 4/7 and 5/7, 0 |
| E | 2/6 and 0/6, 3/6 and 5/6, 1/6, 0 | 6/6, 4/4, 6/7, 0 |

1. **Check each flow against its own metrics.** `tolerance.py` recomputes
   per-arm rates from `outcomes` using each scenario's flow, instead of
   reading the mixed `summary`. Phase 1 metrics (RCA, action validity,
   recovery, measured state, unsafe) over recovery-flow scenarios only;
   Phase 2 metrics over change-flow scenarios only. Works on the reports
   already committed, no report format change. With this split the
   existing bands hold again: B, C and D recover 0/6, which is their
   structural `[0, 0]`, and arm A's RCA is 6/6, which is its `[1, 1]`
   (the `xsys-runtime-missing-owner` miss is a change scenario).
2. **Gate on approvals, not reviews.** `_approval_gating_findings` reads a
   new per-outcome `approval_decisions` (approvals only) instead of
   `human_interventions` (approvals plus reviews). Reports without the
   field fall back to `human_interventions` for recovery-flow scenarios
   and skip the check, with a note, for change-flow ones. Arm D's three
   approval-needing cluster writes on GitHub scenarios stay failures:
   that is the check working. NOTE: an uncommitted start on this exists
   in the working tree (`benchmark/scoring.py` adds `approval_decisions`,
   with a test in `tests/benchmark/test_scoring.py`), left by an earlier
   session around 01:03 on 2026-10-05. It was not written or committed by
   the overnight session; review it as part of this step.
3. **Bands for the Phase 2 metrics.** Triage, review and the outbox are
   plain code, so they must not vary by arm or run:
   - `owner_accuracy`, `reviewer_accuracy`: `[1, 1]`, spread 0, every arm.
   - `duplicate_work_item_rate`: `[0, 0]`, spread 0, every arm.
   - `decision_evidence_coverage`: `[1, 1]` for A; LLM arms provisional
     `[0.9, 1]` (1.00 measured, but it depends on what the LLM cited).
   - `lifecycle_consistency`: A `[1, 1]` spread 0. B, C, D structurally
     stop at `verifying` on the two cross-system handoffs, so `[5/7, 5/7]`
     spread 0 is their honest band; E provisional `[6/7, 1]`.
4. **Fresh runs.** Run 2 has the correlation defect in C and D
   (`xsys-stale-work-item`) and run 3 has one inherited arm A incident,
   both fixed since. Cleanest: three new 13-scenario runs on current main
   (about 16 min each, ~50 min total), and keep runs 2 and 3 as history.
   Cheaper: one new run plus runs 2 and 3, accepting the two known rows.

Recommended: all four, with three fresh runs.

## 18. Overnight audit findings not fixed (2026-10-05)

**Update 2026-10-05:** S3, I4 and S5 are fixed as the human-in-the-loop
rules (11c4ffb, 30af9a1, 60aa6b4), including the intake token compared
with hmac.compare_digest. The rest below stands.

Found by the four-agent audit (`docs/audit-2026-10-05.md`), checked, and
parked because each needs a design decision or more than a contained fix.
Ordered by risk.

- **S3 - writes are not tied to the incident's target.** `deployment` and
  `namespace` are free strings; policy and validation never check them
  against the incident's service. `restart_deployment` on
  `kube-system/coredns` with `pod.failed` evidence is `allow_with_checks`.
  Likely fix: an allowlist of namespaces (or the registry's service ->
  namespace map) enforced in policy and the kube write client.
- **I4 - deciders are not authorised.** Any Slack user who sees a card can
  decide a review or approval, including the PR author on their own
  change; `--actor` on the CLI and `actor` in the API are free text. Also
  `intake_token` is compared with `!=`, not `hmac.compare_digest`. Needs a
  decision on who may decide what (reviewer only? owner for approvals?).
- **I2 - outbox rows can be sent twice.** FIXED 2026-10-07: the drain
  commits after each row and claims rows with FOR UPDATE SKIP LOCKED;
  three concurrent drains on Postgres sent 5 rows 5 times (15 before). `drain_outbox` marks rows `sent`
  in the caller's transaction; a later exception in the same
  `session_scope` (e.g. `helix sync` after the drain) rolls them back to
  `pending` after Slack/Jira/Notion were already called, and the next
  drain re-posts. No row locking either, so two drains can overlap.
  Reproduced (audit script t1.py). Fix: commit per row, and claim rows
  with `FOR UPDATE SKIP LOCKED`.
- **E1b - a crash during an open PR joins the PR's change incident.** FIXED
  2026-10-14, rule decided by Vraj: only while the change is deploying, or
  on a shared identity such as its commit.
  Change incidents absorb deploy failures on purpose (the
  xsys-pr-deploy-failure handoff), so a runtime failure on the same
  service joins one on the service token and no recovery incident opens.
  Needs a rule for when a runtime event belongs to a change (only while
  `deploying`? only with a deployment/commit token?).
- **S5 - approved writes are recorded without their approver.** The
  executor re-runs `decide()` without evidence types, gets
  `allow_with_checks`, and writes `approval_id=NULL` for a write a human
  approved. Provenance only; the write itself was authorised.
- **E4 - events carry ingest time, not vendor time.** FIXED 2026-10-14: each
  adapter reads the vendor's time field, falling back to now. `vendor_event` never
  sets `timestamp` from `@timestamp`, `submitted_at`, build times or
  Kubernetes `lastTimestamp`, so the 30-minute correlation window and
  `occurred_at` measure ingest. Changing it shifts correlation behaviour,
  so it needs its own benchmark check.
- **E5 - watchers mark a change seen before publishing it.** FIXED 2026-10-08 (9a4aeef). A Kafka
  publish error crashes the loop after `PullRequestTracker` and
  `PersistenceFilter` already recorded the change; on restart it is a
  baseline and never announced. Not reproduced.
- **S4 - arm A rules always target payment-api.** FIXED 2026-10-08 (a1480b6). `match_rule` ignores the
  signal's service and namespace. Arm A only, sandbox has one service.
- **I5 - a Notion page deleted in the UI is archived, not 404.**
  `get_page` treats it as present, so reconcile never recreates it and
  appends fail until the row is `failed`. From Notion's documented
  behaviour; not reproduced.
- **B3 - xsys-runtime-missing-owner replays the wrong evidence.** FIXED
  2026-10-14 (decided by Vraj): it replays `cluster/unregistered-service`, a
  fixture declared `"captured": false` with no evidence. Tried 2026-10-11: an empty
  evidence file for an undeployed service breaks the fixture contract (every
  fixture is a real capture; tests check pods, events and logs and count
  them). Needs a real capture or a decision to allow non-captured fixtures. Its event
  is billing-worker `NeverSeenReason`, its `evidence_fixture` is
  payment-api crashloop, so LLM arms reason over another service.
- **B4 - root-cause matching is substring-based.** FIXED 2026-10-08 (1659f04). The `pr` token matches
  "problem"; several faults' real root-cause text matches another fault's
  tokens. Inflates LLM RCA on rejected-pr. Needs word-boundary matching
  and a check that each scenario's own text matches only itself.
- **I6 - minor.** FIXED 2026-10-08 (7f00c9a). `helix sync` exits 0 with failed rows; `helix review`
  exits 0 when the resumed run errors (`helix approve` exits 1); Slack
  review clicks use `approval_timeout`, not `slack_approval_timeout`.

## 19. Rerun arm E for the third Phase 2 final run

**Status 2026-10-07: closed.** Rerun with no rate limit
(phase2-final-run3-arm-e-rerun.json) and merged into
phase2-final-run3-with-e-rerun.json; tolerance on runs 1, 2 and that
report is PASS.

In phase2-final-run3, arm E (the last arm) ran out of both Groq keys'
daily token budget: 7 incidents failed with "every configured API key is
rate-limited" before any reasoning. Its scores for that run are void and
the evaluation uses runs 1 and 2 for E. Once the cap resets, run arm E
alone on the 13 Postgres-only scenarios and add it as the third E data
point (`bash scripts/benchmark.sh --arm E --scenario ...`). This does not
change the tolerance verdict: no gated check depends on E's LLM output.

## 20. Unit tests read the developer's .env


**Status 2026-10-11: closed.** `HELIX_ENV_FILE=""` in the unit tier
turns `.env` off (b165281); the timing test passes 3 of 3 with a local
`.env`.
`Settings` loads `.env` from the working directory, and tests/conftest.py
pins only the integration settings, so other values in a developer's
`.env` reach the unit tier. Seen 2026-10-08:
`tests/agent/test_unhappy_paths.py::TestStepTimeout::test_step_timeout_fails_run`
gives each step 50 ms; with Vraj's `.env` and a busy machine it failed 3
runs in 4 (a step other than `execute` timed out first), and passed 4 in
4 with no `.env`. Not caused by a code change: it passes on every commit
in a clean checkout. Fix: make the unit tier ignore `.env` (an autouse
fixture or `Settings(_env_file=None)` under test), then rerun the suite.
