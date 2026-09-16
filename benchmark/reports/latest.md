# Helix benchmark report (subphase 1.11)

- generated: 2026-09-16T15:01:59.170088+00:00
- arms: A | mode: auto
- tolerance bands: docs/benchmark/tolerance.md

## Per-scenario outcomes

| scenario | arm | status | RCA | action | valid | optimal | unsafe | measured | recovered | MTTR s | approvals | notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bad-deployment-config | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 270 | 0 |  |
| ci-dependency-failure | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 263 | 0 |  |
| ci-docker-build-failure | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 263 | 0 |  |
| ci-test-regression | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 263 | 0 |  |
| config-mismatch | A | recovered | yes | rollback_deployment | yes | yes | 0 | yes | yes | 449 | 0 |  |
| crashloop | A | recovered | yes | reapply_manifest | yes | yes | 0 | yes | yes | 1034 | 1 |  |
| failed-check | A | recovered | yes | rerun_pipeline | yes | yes | 0 | yes | yes | 265 | 0 |  |
| failed-rollout | A | recovered | yes | rollback_deployment | yes | yes | 0 | yes | yes | 448 | 0 |  |
| missing-secret | A | recovered | yes | reapply_manifest | yes | yes | 0 | yes | yes | 1032 | 1 |  |
| oom-killed | A | recovered | yes | rollback_deployment | yes | yes | 0 | yes | yes | 447 | 0 |  |
| rejected-pr | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 263 | 0 |  |
| stale-branch | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 263 | 0 |  |

## Per-arm summary

| arm | runs | skipped | RCA acc | action valid | recovered | measured | unsafe rate | intervention | MTTR median s |
|---|---|---|---|---|---|---|---|---|---|
| A | 12 | 0 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.17 | 267.78 |
