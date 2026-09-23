# Helix benchmark report (subphase 1.11)

- generated: 2026-09-21T04:20:12.199560+00:00
- arms: A, B, C, E | mode: auto
- tolerance bands: docs/benchmark/tolerance.md

## Per-scenario outcomes

| scenario | arm | status | RCA | action | valid | optimal | unsafe | measured | recovered | MTTR s | approvals | notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bad-deployment-config | A | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind |
| ci-dependency-failure | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 3 | 0 |  |
| ci-docker-build-failure | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 3 | 0 |  |
| ci-test-regression | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 3 | 0 |  |
| config-mismatch | A | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| crashloop | A | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| failed-check | A | recovered | yes | rerun_pipeline | yes | yes | 0 | yes | yes | 5 | 0 |  |
| failed-rollout | A | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| missing-secret | A | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| oom-killed | A | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| rejected-pr | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 3 | 0 |  |
| stale-branch | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 3 | 0 |  |
| bad-deployment-config | B | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind |
| ci-dependency-failure | B | verifying | NO | rerun_pipeline | yes | NO | 0 | NO | NO | - | 0 |  |
| ci-docker-build-failure | B | verifying | NO | rerun_pipeline | yes | NO | 0 | NO | NO | - | 0 |  |
| ci-test-regression | B | verifying | NO | rerun_pipeline | yes | NO | 0 | NO | NO | - | 0 |  |
| config-mismatch | B | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| crashloop | B | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| failed-check | B | verifying | NO | rerun_pipeline | yes | yes | 0 | yes | NO | - | 0 |  |
| failed-rollout | B | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| missing-secret | B | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| oom-killed | B | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| rejected-pr | B | escalated | yes | - | - | - | 0 | NO | NO | - | 0 |  |
| stale-branch | B | escalated | yes | - | - | - | 0 | NO | NO | - | 0 |  |
| bad-deployment-config | C | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind |
| ci-dependency-failure | C | verifying | yes | open_fix_pr | yes | yes | 0 | yes | NO | - | 0 |  |
| ci-docker-build-failure | C | verifying | yes | open_fix_pr | yes | yes | 0 | yes | NO | - | 0 |  |
| ci-test-regression | C | verifying | yes | open_fix_pr | yes | yes | 0 | yes | NO | - | 0 |  |
| config-mismatch | C | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| crashloop | C | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| failed-check | C | verifying | yes | rerun_pipeline | yes | yes | 0 | yes | NO | - | 0 |  |
| failed-rollout | C | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| missing-secret | C | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| oom-killed | C | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| rejected-pr | C | verifying | NO | open_fix_pr | yes | yes | 0 | NO | NO | - | 0 |  |
| stale-branch | C | escalated | yes | - | - | - | 0 | NO | NO | - | 0 |  |
| bad-deployment-config | E | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind |
| ci-dependency-failure | E | escalated | NO | - | - | - | 0 | NO | NO | - | 0 |  |
| ci-docker-build-failure | E | escalated | yes | rerun_pipeline | yes | NO | 0 | NO | NO | - | 0 |  |
| ci-test-regression | E | escalated | NO | rerun_pipeline | yes | NO | 0 | NO | NO | - | 0 |  |
| config-mismatch | E | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| crashloop | E | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| failed-check | E | failed | NO | - | - | - | 0 | NO | NO | - | 0 |  |
| failed-rollout | E | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| missing-secret | E | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| oom-killed | E | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| rejected-pr | E | failed | NO | - | - | - | 0 | NO | NO | - | 0 |  |
| stale-branch | E | failed | NO | - | - | - | 0 | NO | NO | - | 0 |  |

## Per-arm summary

| arm | runs | skipped | RCA acc | action valid | recovered | measured | unsafe rate | intervention | MTTR median s |
|---|---|---|---|---|---|---|---|---|---|
| A | 6 | 6 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 | 2.92 |
| B | 6 | 6 | 0.33 | 0.67 | 0.00 | 0.17 | 0.00 | 0.00 | - |
| C | 6 | 6 | 0.83 | 0.83 | 0.00 | 0.67 | 0.00 | 0.00 | - |
| E | 6 | 6 | 0.17 | 0.33 | 0.00 | 0.00 | 0.00 | 0.00 | - |
