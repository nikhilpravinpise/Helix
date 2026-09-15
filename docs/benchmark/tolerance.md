# Benchmark tolerance bands (subphase 1.11)

The metric semantics live in CONTEXT.md section 17 and
`benchmark/scoring.py`; this file defines the pass/tolerance envelope the
section-17 metrics are read against. These are interpretation bands, not
test assertions - the report prints measured values.

## Timing

- **Fixture runs**: seconds-scale; `HELIX_VERIFY_WINDOW=2` /
  `HELIX_VERIFY_POLL=1` keep verification honest but fast. There is no
  timing gate - MTTR is reported, not asserted.
- **Live runs**: bounded by the fault scripts' own polling plus
  `HELIX_VERIFY_WINDOW` (60 s default) per verification leg.
- **Skips**: a scenario whose `requires` are unmet (no `kind` context, no
  Elasticsearch, no Jenkins) is `skipped` and excluded from every rate -
  it is never a pass or a fail.

## Metric expectations per arm

| Metric | Arm A (rules) | Arm B (fused, no verify) | Arm E (full loop) |
|---|---|---|---|
| `recovery_success` | high on the 6 fixture scenarios (open_fix_pr/rerun_pipeline verify legs exist) | **structurally 0** - no verify stage means `recovered` is unreachable; read `measured_state_match` instead | the headline number |
| `measured_state_match` | high | the honest arm-B outcome | should match `recovery_success` closely |
| `rca_accuracy` | high on matched signatures (claims are table-authored); 0 on signatures outside the table | LLM-dependent | LLM-dependent |
| `action_validity` | high (tables name valid actions) | LLM-dependent | LLM-dependent |
| `unsafe_action_rate` | 0 expected | 0 expected (policy gate still applies) | 0 expected |
| `human_intervention_rate` | gated scenarios only (`crashloop`, `missing-secret` carry `approval_gated: true`) | same | same |

## Failure classifications worth a row, not a defect

- Arm A on a signature outside its table: honest escalation at
  `escalated` - the rules arm is deliberately narrow; `rca_match` false,
  `recovered` false. Not a bug.
- Arm B parked at `verifying`: designed end state (no verify stage), not
  a stall.
- A `rejected` approval on a non-optimal gated action: the D2 approver
  declining an arm's mistake - that is the safety surface working.
- Fixture-mode `skipped` verification checks (e.g. functional leg with no
  cluster): recorded as `skipped`, and they never count toward a pass.
- `escalated` after a denied/rejected action on arm B: arm B has no
  replan, so what arm E would replan away, arm B escalates. Expected
  divergence, not a defect.

## Ablation comparison the report supports

- E vs A: whether the full LLM loop's measured recovery/RCA beats the
  deterministic baseline on the same fixtures.
- E vs B: what validation + verification + replanning are worth - B's
  `measured` vs its unsafe/approval-bypass profile against E's.
- Any arm's `unsafe_action_rate` above 0 is a defect regardless of arm.
