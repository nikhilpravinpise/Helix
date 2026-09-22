# Operator Runbook

How a human follows an incident through Helix - CLI plus the operator UI
(subphase 1.12, server-rendered per D11). Both surfaces talk to the same
Postgres state; nothing here needs the workflow driver to be a specific
process.

## Bring-up

```bash
make db-up          # compose Postgres (infra/postgres-compose.yml)
make db-migrate     # alembic upgrade head
make serve          # uv run helix serve -> http://127.0.0.1:8787
```

`helix serve` exposes the JSON API and the UI from one process:

- `GET /` redirects to the UI incident list at `/ui/`
- `/ui/` - incident list
- `/ui/incidents/INC-*` - incident detail with the full provenance timeline
- `/ui/approvals` - the approval queue
- `/static/*` - vendored CSS/JS (no CDN, no build step)

The JSON API is unchanged: `/intake/*`, `/incidents`, `/approvals` still
serve JSON to non-browser clients.

## Feeding Helix a failure signal

```bash
# From a file (or stdin with '-'):
uv run helix intake --file signal.json
# -> prints the incident id, e.g. INC-83bbd83e

# Record AND drive the workflow in one step:
uv run helix intake --file signal.json --run

# Same thing over HTTP (what scripts/emit-signal.sh does):
curl -X POST http://127.0.0.1:8787/intake/kubernetes \
  -H 'Content-Type: application/json' -d @signal.json
```

Repeat signals for the same fault attach to the open incident rather
than creating duplicates.

## Driving an incident (CLI)

```bash
uv run helix incidents          # list all incidents, newest first
uv run helix run INC-83bbd83e   # drive the workflow to its next stop:
                                # recovered / escalated / failed, or
                                # suspended at awaiting_approval
uv run helix provenance INC-83bbd83e   # full decision record as Markdown
```

`helix run` is idempotent at the boundaries: a terminal incident is a
no-op, a suspended one resumes, a mid-flight one keeps moving.

## Following an incident (UI)

Open `http://127.0.0.1:8787/` and click the incident id. The detail page
shows:

- header: service, environment, severity, status badge, attempt count,
  linked PR/deployment, timestamps, fingerprint, escalation cause when
  present
- provenance timeline in CONTEXT.md section-15 order: trigger ->
  evidence -> hypothesis -> candidates -> decision -> validation ->
  approval -> execution -> verification - every entry resolving to a
  stored row id and timestamp
- the persisted status transitions

While the incident is non-terminal the page polls `GET /incidents/{id}`
every 3 seconds and reloads when the status changes, so a `helix run`
(or the API intake hook) driving it in another process shows up live.
Polling stops at a terminal status (recovered / escalated / failed).

## Approvals

When policy marks an action REQUIRE_APPROVAL (production targets,
secret changes), the run suspends at `awaiting_approval` with a pending
approvals row.

CLI:

```bash
uv run helix approvals                          # pending rows
uv run helix approve INC-83bbd83e               # actor = HELIX_OPERATOR
uv run helix approve INC-83bbd83e --actor nikhil --note "lgtm"
uv run helix reject  INC-83bbd83e --note "wait for freeze"
```

UI: open `/ui/approvals` (the nav badge shows the pending count). Each
pending card shows the action, params, risk tier, linked incident, and
how long it has been waiting. Set the Operator name once (it persists in
the browser session), add an optional note, and click Approve or Reject -
the button POSTs to `POST /approvals/{id}/approve|reject`, which records
the decision and resumes the suspended run; the page reloads showing the
decision. Rejection sends the incident back to planning with that exact
(action, params) fingerprint excluded. Decided rows stay listed with who
decided and when.

If `HELIX_INTAKE_TOKEN` is set, decision POSTs are gated too: the page
shows an Intake token field and the browser sends it as a Bearer header.

## Change reviews (Phase 2)

`change` incidents (PR/push events) park at `review_pending` with a
pending review row instead of an approvals row:

```bash
uv run helix reviews                        # pending review requests
uv run helix review INC-83bbd83e approve --actor alice --note "lgtm"
uv run helix review INC-83bbd83e reject --note "needs test"
```

Approve sends the change to `deploying`; reject drives
`rejected -> fix_required`, and the author's fix returns to
`review_pending`. Review buttons on the Slack review request and
`POST /reviews/{id}/approve|reject` take the same path.

## Integration drain and drift (Phase 2)

Every transition's external side effects commit as `integration_outbox`
rows in the same transaction as the status change; delivery happens
after, in the drain:

```bash
uv run helix sync          # deliver Slack/work-item/Notion ops, reconcile drift, re-derive the graph
uv run helix graph-sync    # re-derive the Neo4j graph only (needs HELIX_GRAPH_ENABLED=true)
```

Unconfigured integrations record `skipped`, never a failure -
PostgreSQL stays the source of truth. `helix sync` also recreates
drifted remotes (deleted Jira issues / Notion pages) and refreshes
work-item status. Phase 2 bring-up (Slack/Jira/Notion/Neo4j variables)
lives in [docs/bring-up.md](../bring-up.md) §6.5.

## Reading state without the UI

```bash
curl http://127.0.0.1:8787/incidents                 # JSON list
curl http://127.0.0.1:8787/incidents/INC-83bbd83e    # detail + signals + transitions
curl http://127.0.0.1:8787/incidents/INC-83bbd83e/provenance   # provenance JSON
curl http://127.0.0.1:8787/approvals?status=pending  # pending approvals JSON
```
