# Helix benchmark report (subphase 1.11)

- generated: 2026-09-15T21:12:09.690818+00:00
- arms: A, B, E | mode: auto
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
| failed-check | A | recovered | yes | rerun_pipeline | yes | yes | 0 | yes | yes | 4 | 0 |  |
| failed-rollout | A | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| missing-secret | A | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| oom-killed | A | SKIPPED | - | - | - | - | - | - | - | - | - | unmet requirements: kind, elasticsearch |
| rejected-pr | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 2 | 0 |  |
| stale-branch | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 2 | 0 |  |
| bad-deployment-config | B | SKIPPED | - | - | - | - | - | - | - | - | - | arm B requires HELIX_LLM_API_KEY |
| ci-dependency-failure | B | SKIPPED | - | - | - | - | - | - | - | - | - | arm B requires HELIX_LLM_API_KEY |
| ci-docker-build-failure | B | SKIPPED | - | - | - | - | - | - | - | - | - | arm B requires HELIX_LLM_API_KEY |
| ci-test-regression | B | SKIPPED | - | - | - | - | - | - | - | - | - | arm B requires HELIX_LLM_API_KEY |
| config-mismatch | B | SKIPPED | - | - | - | - | - | - | - | - | - | arm B requires HELIX_LLM_API_KEY |
| crashloop | B | SKIPPED | - | - | - | - | - | - | - | - | - | arm B requires HELIX_LLM_API_KEY |
| failed-check | B | SKIPPED | - | - | - | - | - | - | - | - | - | arm B requires HELIX_LLM_API_KEY |
| failed-rollout | B | SKIPPED | - | - | - | - | - | - | - | - | - | arm B requires HELIX_LLM_API_KEY |
| missing-secret | B | SKIPPED | - | - | - | - | - | - | - | - | - | arm B requires HELIX_LLM_API_KEY |
| oom-killed | B | SKIPPED | - | - | - | - | - | - | - | - | - | arm B requires HELIX_LLM_API_KEY |
| rejected-pr | B | SKIPPED | - | - | - | - | - | - | - | - | - | arm B requires HELIX_LLM_API_KEY |
| stale-branch | B | SKIPPED | - | - | - | - | - | - | - | - | - | arm B requires HELIX_LLM_API_KEY |
| bad-deployment-config | E | SKIPPED | - | - | - | - | - | - | - | - | - | arm E requires HELIX_LLM_API_KEY |
| ci-dependency-failure | E | SKIPPED | - | - | - | - | - | - | - | - | - | arm E requires HELIX_LLM_API_KEY |
| ci-docker-build-failure | E | SKIPPED | - | - | - | - | - | - | - | - | - | arm E requires HELIX_LLM_API_KEY |
| ci-test-regression | E | SKIPPED | - | - | - | - | - | - | - | - | - | arm E requires HELIX_LLM_API_KEY |
| config-mismatch | E | SKIPPED | - | - | - | - | - | - | - | - | - | arm E requires HELIX_LLM_API_KEY |
| crashloop | E | SKIPPED | - | - | - | - | - | - | - | - | - | arm E requires HELIX_LLM_API_KEY |
| failed-check | E | SKIPPED | - | - | - | - | - | - | - | - | - | arm E requires HELIX_LLM_API_KEY |
| failed-rollout | E | SKIPPED | - | - | - | - | - | - | - | - | - | arm E requires HELIX_LLM_API_KEY |
| missing-secret | E | SKIPPED | - | - | - | - | - | - | - | - | - | arm E requires HELIX_LLM_API_KEY |
| oom-killed | E | SKIPPED | - | - | - | - | - | - | - | - | - | arm E requires HELIX_LLM_API_KEY |
| rejected-pr | E | SKIPPED | - | - | - | - | - | - | - | - | - | arm E requires HELIX_LLM_API_KEY |
| stale-branch | E | SKIPPED | - | - | - | - | - | - | - | - | - | arm E requires HELIX_LLM_API_KEY |

## Per-arm summary

| arm | runs | skipped | RCA acc | action valid | recovered | measured | unsafe rate | intervention | MTTR median s |
|---|---|---|---|---|---|---|---|---|---|
| A | 6 | 6 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 | 2.54 |
| B | 0 | 12 | - | - | - | - | - | - | - |
| E | 0 | 12 | - | - | - | - | - | - | - |
