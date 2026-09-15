# Decision Provenance (subphase 1.9)

"How did the system arrive at this action and this outcome?" - the
answer must be reconstructable from stored state alone, so
`state/provenance.py` assembles one ordered record per incident by
reading its persisted rows. No external calls, no writes, no dependence
on transient LangGraph state: `assemble_provenance(session, incident_id)`
takes a session and an INC-* id and returns a JSON-serializable record.

## The record

```text
incident   - id, service, environment, severity, status, attempts,
             fingerprint, linked PR/deployment, created/updated
stages     - the CONTEXT.md section 15 chain, in order:
  trigger      - the signal rows that opened/attached to the incident
  evidence     - collected Evidence rows (id, source, kind, summary,
                 origin live|fixture, collected_at)
  hypothesis   - recorded claims (claim, cited refs, confidence, attempt)
  candidates   - the FULL ranked list per plan pass (rank, score,
                 components breakdown, risk_tier, selected flag)
  decision     - the derived stage: newest selected candidate + the
                 policy verdict and approval row that governed it,
                 resolved through executions/approvals candidate_id FKs
  validation   - per-candidate verdicts (outcome, reason, details)
  approval     - the gate's rows (status, decided_by, decided_at, note)
  execution    - audit rows (result, policy_decision, origin,
                 decided_by, started/finished, output)
  verification - verdicts (outcome, per-check record, window seconds,
                 needs_compensation)
transitions - the status timeline from the transitions table
```

Every entry carries its stored row id and timestamp. `decision` is the
one derived stage - there is no `decisions` table - but it still only
points at stored rows (the selected `candidates` row plus the
`executions`/`approvals` rows carrying its `candidate_id`).

## Surfaces

- `GET /incidents/{id}/provenance` (helix/api.py) returns the record as
  JSON; 404 on an unknown incident.
- `helix provenance INC-*` (helix/cli.py) prints
  `render_markdown(record)` - the same record as a readable timeline:
  section per stage, one line per row, transitions at the end.

## Why it is shaped this way

Phase 1.12's operator timeline renders this record directly - the JSON
shape is the data source, so the UI never re-derives provenance from
workflow memory. It is also the audit handoff the phase closes on: a
reader can walk trigger -> evidence -> hypothesis -> candidates ->
decision -> validation -> approval -> execution -> verification ->
transitions and see exactly which stored rows justified each step,
including the failure paths (failed validations, refused executions,
failed verifications, `needs_compensation` debts).
