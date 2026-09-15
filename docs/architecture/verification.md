# Verification Agent (subphase 1.9)

The `verify` node answers the question the run's outcome depends on:
**did the executed action actually restore the desired state?** It is a
worker node, not an executor: it reads evidence capabilities, samples
desired-state checks across a stability window, persists the verdict, and
returns to the coordinator. It never calls a write transport - the only
"action" it performs is the functional probe's own HTTP/browser request
against the service, which is an observation, not a mutation.

## What "verified" means

Verification is **desired state held across a stability window**, not a
single green snapshot. Each sampled check is polled every
`HELIX_VERIFY_POLL` seconds for `HELIX_VERIFY_WINDOW` seconds:

- A check that has never been healthy by window end fails - the action
  gets a grace period to land, then time runs out.
- A check that was healthy and then fails is **flapping** - the restart
  that crash-loops again two minutes later is not a recovery, and the
  check records it distinctly.
- A sample that cannot be taken (capability error) is ERROR. Any ERROR
  during the window means stability is unconfirmed - the window fails
  rather than trusting the samples that did land.
- SKIPPED checks (fixture mode has no live service to probe, or the
  fixture carries no document for that leg) do not count - but they are
  recorded as `skipped`, never reported as pass.

The legs are assembled per action family in `agent/verification/`:

| Action family | Legs |
|---|---|
| `restart_deployment`, `rollback_deployment`, `scale_deployment`, `reapply_manifest` (k8s targets) | `pods_ready` (all `app=<service>` pods Running, containers ready, no bad waiting state; restart-count rise or a Ready pod disappearing across samples is a failed sample), `rollout_complete` (deployment replicas ready/updated/available, no ProgressDeadlineExceeded), `elk_errors` (post-action `level:error` count <= `HELIX_ELK_ERROR_THRESHOLD`), `functional` (live mode: port-forward + HTTP GET /health + Selenium `/docs` render + `/payments` round trip; fixture mode: recorded `skipped`), plus `secret_present` when the manifest's object is a Secret |
| `rerun_pipeline` | `build_green` (the job's latest build result is SUCCESS) |
| `open_fix_pr` | `fix_pr_exists` (live: the PR exists, is open, on the right branch; fixture: the recorded intended write covers the intended files and branch - the PR IS the remediation) |

## Verdicts and routing

`state.models.VerificationOutcome` - `pass` or `fail`. The coordinator
owns what happens next:

| Outcome | Action reversibility | Coordinator does |
|---|---|---|
| `pass` | any | `verifying -> recovered` |
| `fail` | `stateless` | `verifying -> remediation_pending`, replan with the fingerprint excluded |
| `fail` | `compensable` | the `compensate` node runs the undo first (1.10), then the same bounded replan - see [unhappy-paths.md](unhappy-paths.md) |
| `fail` | `irreversible` | `verifying -> escalated` - never retried |

The outcome is deliberately binary: `error` and `skipped` exist only at
the per-check level. A window where every leg errored or skipped folds to
`fail` - "verification could not run" is not recovery, and the replan
path is the honest answer (a next run may verify again if the failure was
transient infrastructure).

A failed verification's fingerprint joins `excluded_candidates` - the
same mechanism validation uses - so a re-planned pass can never
re-propose the action that just proved it does not restore the state.

## The verifications row

Every verify pass that produced a verdict writes one `verifications`
row: incident/execution/candidate links, the action + params +
fingerprint checked, the attempt number, the windowed per-check record
(statuses, details, sample details, evidence ids), start/finish
timestamps, and `needs_compensation`. This row is the provenance
record's verification stage and the audit of *why* a run ended
recovered, replanned, or escalated.

## The functional leg

Live mode only. `agent/verification/functional.py` opens a
`kubectl port-forward` to the service (context-manager style: the
process is always terminated, escalate-to-kill if it stalls), waits for
the local port, then probes `/health` over HTTP and - when Selenium is
available - renders `/docs` in a headless browser and drives a
`POST /payments` round trip. Selenium absence degrades the browser probe
to a `/docs` HTTP fetch, never to a skip: the health probe still proves
the service answers. The whole leg is `skipped` only when there is no
live service to probe (fixture mode, or `HELIX_VERIFY_FUNCTIONAL=false`
disables the network probes explicitly).
