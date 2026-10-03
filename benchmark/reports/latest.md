# Helix benchmark report (subphase 1.11)

- generated: 2026-10-02T07:01:59.805677+00:00
- arms: A, B, C, D, E | mode: auto
- tolerance bands: docs/benchmark/tolerance.md

## Per-scenario outcomes

| scenario | arm | status | RCA | action | valid | optimal | unsafe | measured | recovered | MTTR s | approvals | notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bad-deployment-config | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 7 | 0 |  |
| ci-dependency-failure | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 2 | 0 |  |
| ci-docker-build-failure | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 2 | 0 |  |
| ci-test-regression | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 2 | 0 |  |
| config-mismatch | A | recovered | yes | rollback_deployment | yes | yes | 0 | yes | yes | 184 | 0 |  |
| crashloop | A | recovered | yes | reapply_manifest | yes | yes | 0 | yes | yes | 245 | 1 |  |
| failed-check | A | recovered | yes | rerun_pipeline | yes | yes | 0 | yes | yes | 4 | 0 |  |
| failed-rollout | A | recovered | yes | rollback_deployment | yes | yes | 0 | yes | yes | 183 | 0 |  |
| missing-secret | A | recovered | yes | reapply_manifest | yes | yes | 0 | yes | yes | 245 | 1 |  |
| oom-killed | A | recovered | yes | rollback_deployment | yes | yes | 0 | yes | yes | 183 | 0 |  |
| org-duplicate-change | A | escalated | - | - | - | - | 0 | yes | yes | - | 0 |  |
| org-known-owner-change | A | recovered | - | - | - | - | 0 | yes | yes | 9 | 1 |  |
| org-missing-owner | A | escalated | - | - | - | - | 0 | yes | yes | - | 0 |  |
| org-reject-rereview | A | recovered | - | - | - | - | 0 | yes | yes | 15 | 2 |  |
| rejected-pr | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 2 | 0 |  |
| stale-branch | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 2 | 0 |  |
| xsys-pr-deploy-failure | A | recovered | yes | open_fix_pr | yes | yes | 0 | yes | yes | 10 | 1 |  |
| xsys-runtime-missing-owner | A | escalated | NO | - | - | - | 0 | yes | yes | - | 0 |  |
| xsys-stale-work-item | A | recovered | - | - | - | - | 0 | yes | yes | 9 | 1 |  |
| bad-deployment-config | B | verifying | yes | open_fix_pr | yes | yes | 0 | NO | NO | - | 0 |  |
| ci-dependency-failure | B | verifying | NO | rerun_pipeline | yes | NO | 0 | NO | NO | - | 0 |  |
| ci-docker-build-failure | B | verifying | NO | rerun_pipeline | yes | NO | 0 | NO | NO | - | 0 |  |
| ci-test-regression | B | verifying | NO | rerun_pipeline | yes | NO | 0 | NO | NO | - | 0 |  |
| config-mismatch | B | verifying | yes | rollback_deployment | yes | yes | 0 | yes | NO | - | 0 |  |
| crashloop | B | verifying | NO | restart_deployment | yes | NO | 0 | NO | NO | - | 0 |  |
| failed-check | B | verifying | yes | rerun_pipeline | yes | yes | 0 | yes | NO | - | 0 |  |
| failed-rollout | B | verifying | yes | rollback_deployment | yes | yes | 0 | yes | NO | - | 0 |  |
| missing-secret | B | escalated | yes | - | NO | - | 0 | NO | NO | - | 0 |  |
| oom-killed | B | verifying | yes | restart_deployment | NO | NO | 1 | NO | NO | - | 0 |  |
| org-duplicate-change | B | escalated | - | - | - | - | 0 | yes | yes | - | 0 |  |
| org-known-owner-change | B | recovered | - | - | - | - | 0 | yes | yes | 15 | 1 |  |
| org-missing-owner | B | escalated | - | - | - | - | 0 | yes | yes | - | 0 |  |
| org-reject-rereview | B | recovered | - | - | - | - | 0 | yes | yes | 23 | 2 |  |
| rejected-pr | B | verifying | yes | open_fix_pr | yes | yes | 0 | NO | NO | - | 0 |  |
| stale-branch | B | escalated | yes | - | NO | - | 0 | NO | NO | - | 0 |  |
| xsys-pr-deploy-failure | B | verifying | NO | rerun_pipeline | yes | NO | 0 | NO | NO | - | 1 |  |
| xsys-runtime-missing-owner | B | verifying | NO | restart_deployment | NO | NO | 0 | NO | NO | - | 0 |  |
| xsys-stale-work-item | B | verifying | NO | rerun_pipeline | NO | NO | 0 | NO | NO | - | 1 |  |
| bad-deployment-config | C | verifying | yes | open_fix_pr | yes | yes | 0 | NO | NO | - | 0 |  |
| ci-dependency-failure | C | verifying | yes | open_fix_pr | yes | yes | 0 | yes | NO | - | 0 |  |
| ci-docker-build-failure | C | verifying | NO | open_fix_pr | yes | yes | 0 | yes | NO | - | 0 |  |
| ci-test-regression | C | verifying | yes | open_fix_pr | yes | yes | 0 | yes | NO | - | 0 |  |
| config-mismatch | C | verifying | yes | rollback_deployment | yes | yes | 0 | yes | NO | - | 0 |  |
| crashloop | C | escalated | yes | - | NO | - | 0 | NO | NO | - | 0 |  |
| failed-check | C | verifying | yes | rerun_pipeline | yes | yes | 0 | yes | NO | - | 0 |  |
| failed-rollout | C | verifying | yes | rollback_deployment | yes | yes | 0 | yes | NO | - | 0 |  |
| missing-secret | C | escalated | yes | - | NO | - | 0 | NO | NO | - | 0 |  |
| oom-killed | C | verifying | yes | rollback_deployment | yes | yes | 0 | yes | NO | - | 0 |  |
| org-duplicate-change | C | escalated | - | - | - | - | 0 | yes | yes | - | 0 |  |
| org-known-owner-change | C | recovered | - | - | - | - | 0 | yes | yes | 11 | 1 |  |
| org-missing-owner | C | escalated | - | - | - | - | 0 | yes | yes | - | 0 |  |
| org-reject-rereview | C | recovered | - | - | - | - | 0 | yes | yes | 17 | 2 |  |
| rejected-pr | C | verifying | NO | open_fix_pr | yes | yes | 0 | NO | NO | - | 0 |  |
| stale-branch | C | escalated | yes | - | NO | - | 0 | NO | NO | - | 0 |  |
| xsys-pr-deploy-failure | C | verifying | yes | open_fix_pr | yes | yes | 0 | NO | NO | - | 1 |  |
| xsys-runtime-missing-owner | C | verifying | NO | restart_deployment | NO | NO | 0 | NO | NO | - | 0 |  |
| xsys-stale-work-item | C | verifying | NO | open_fix_pr | NO | NO | 0 | NO | NO | - | 1 |  |
| bad-deployment-config | D | verifying | yes | open_fix_pr | yes | yes | 0 | NO | NO | - | 0 |  |
| ci-dependency-failure | D | verifying | NO | rerun_pipeline | yes | NO | 0 | NO | NO | - | 0 |  |
| ci-docker-build-failure | D | verifying | NO | rerun_pipeline | yes | NO | 0 | NO | NO | - | 0 |  |
| ci-test-regression | D | verifying | NO | rerun_pipeline | yes | NO | 0 | NO | NO | - | 0 |  |
| config-mismatch | D | verifying | yes | rollback_deployment | yes | yes | 0 | yes | NO | - | 0 |  |
| crashloop | D | verifying | NO | restart_deployment | yes | NO | 0 | NO | NO | - | 0 |  |
| failed-check | D | verifying | NO | rerun_pipeline | yes | yes | 0 | yes | NO | - | 0 |  |
| failed-rollout | D | verifying | yes | rollback_deployment | yes | yes | 0 | yes | NO | - | 0 |  |
| missing-secret | D | escalated | NO | - | NO | - | 0 | NO | NO | - | 0 |  |
| oom-killed | D | verifying | yes | restart_deployment | NO | NO | 1 | NO | NO | - | 0 |  |
| org-duplicate-change | D | escalated | - | - | - | - | 0 | yes | yes | - | 0 |  |
| org-known-owner-change | D | recovered | - | - | - | - | 0 | yes | yes | 15 | 1 |  |
| org-missing-owner | D | escalated | - | - | - | - | 0 | yes | yes | - | 0 |  |
| org-reject-rereview | D | recovered | - | - | - | - | 0 | yes | yes | 25 | 2 |  |
| rejected-pr | D | escalated | NO | rollback_deployment | NO | NO | 0 | NO | NO | - | 1 |  |
| stale-branch | D | escalated | yes | rollback_deployment | NO | NO | 0 | NO | NO | - | 1 |  |
| xsys-pr-deploy-failure | D | verifying | NO | open_fix_pr | yes | yes | 0 | NO | NO | - | 1 |  |
| xsys-runtime-missing-owner | D | verifying | NO | rollback_deployment | NO | NO | 0 | NO | NO | - | 0 |  |
| xsys-stale-work-item | D | verifying | NO | open_fix_pr | NO | NO | 0 | NO | NO | - | 1 |  |
| bad-deployment-config | E | escalated | yes | open_fix_pr | yes | yes | 0 | NO | NO | - | 0 |  |
| ci-dependency-failure | E | recovered | NO | open_fix_pr | yes | yes | 0 | NO | NO | 45 | 0 |  |
| ci-docker-build-failure | E | escalated | NO | open_fix_pr | yes | yes | 0 | NO | NO | - | 0 |  |
| ci-test-regression | E | recovered | NO | open_fix_pr | yes | yes | 0 | NO | NO | 61 | 0 |  |
| config-mismatch | E | recovered | yes | rollback_deployment | yes | yes | 0 | yes | yes | 198 | 0 |  |
| crashloop | E | escalated | yes | restart_deployment | yes | NO | 0 | NO | NO | - | 0 |  |
| failed-check | E | recovered | NO | open_fix_pr | yes | NO | 0 | NO | NO | 11 | 0 |  |
| failed-rollout | E | SKIPPED | - | - | - | - | - | - | - | - | - | injection failed: reset exited 1: error: timed out waiting for the condition |
| missing-secret | E | recovered | yes | open_fix_pr | yes | NO | 0 | NO | NO | 16 | 0 |  |
| oom-killed | E | failed | NO | - | NO | - | 0 | NO | NO | - | 0 |  |
| org-duplicate-change | E | escalated | - | - | - | - | 0 | yes | yes | - | 0 |  |
| org-known-owner-change | E | recovered | - | - | - | - | 0 | yes | yes | 13 | 1 |  |
| org-missing-owner | E | escalated | - | - | - | - | 0 | yes | yes | - | 0 |  |
| org-reject-rereview | E | recovered | - | - | - | - | 0 | yes | yes | 23 | 2 |  |
| rejected-pr | E | failed | NO | - | NO | - | 0 | NO | NO | - | 0 |  |
| stale-branch | E | failed | yes | - | NO | - | 0 | NO | NO | - | 0 |  |
| xsys-pr-deploy-failure | E | failed | - | - | - | - | 0 | NO | NO | - | 1 |  |
| xsys-runtime-missing-owner | E | failed | - | - | - | - | 0 | yes | NO | - | 0 |  |
| xsys-stale-work-item | E | recovered | - | - | - | - | 0 | yes | yes | 13 | 1 |  |

## Change-flow detail (Phase 2)

| scenario | arm | status | owner | reviewer | lifecycle | dup items | evidence cov | interventions |
|---|---|---|---|---|---|---|---|---|
| bad-deployment-config | A | recovered | - | - | yes | 0 | 1.00 | 0 |
| ci-dependency-failure | A | recovered | - | - | yes | 0 | 1.00 | 0 |
| ci-docker-build-failure | A | recovered | - | - | yes | 0 | 1.00 | 0 |
| ci-test-regression | A | recovered | - | - | yes | 0 | 1.00 | 0 |
| config-mismatch | A | recovered | - | - | yes | 0 | 1.00 | 0 |
| crashloop | A | recovered | - | - | yes | 0 | 1.00 | 1 |
| failed-check | A | recovered | - | - | yes | 0 | 1.00 | 0 |
| failed-rollout | A | recovered | - | - | yes | 0 | 1.00 | 0 |
| missing-secret | A | recovered | - | - | yes | 0 | 1.00 | 1 |
| oom-killed | A | recovered | - | - | yes | 0 | 1.00 | 0 |
| org-duplicate-change | A | escalated | yes | - | yes | 0 | - | 0 |
| org-known-owner-change | A | recovered | yes | yes | yes | 0 | - | 1 |
| org-missing-owner | A | escalated | yes | - | yes | 0 | - | 0 |
| org-reject-rereview | A | recovered | yes | yes | yes | 0 | - | 2 |
| rejected-pr | A | recovered | - | - | yes | 0 | 1.00 | 0 |
| stale-branch | A | recovered | - | - | yes | 0 | 1.00 | 0 |
| xsys-pr-deploy-failure | A | recovered | yes | yes | yes | 0 | 1.00 | 1 |
| xsys-runtime-missing-owner | A | escalated | - | - | yes | 0 | - | 0 |
| xsys-stale-work-item | A | recovered | yes | yes | yes | 0 | - | 1 |
| bad-deployment-config | B | verifying | - | - | yes | 0 | 1.00 | 0 |
| ci-dependency-failure | B | verifying | - | - | yes | 0 | 1.00 | 0 |
| ci-docker-build-failure | B | verifying | - | - | yes | 0 | 1.00 | 0 |
| ci-test-regression | B | verifying | - | - | yes | 0 | 1.00 | 0 |
| config-mismatch | B | verifying | - | - | yes | 0 | 1.00 | 0 |
| crashloop | B | verifying | - | - | yes | 0 | 1.00 | 0 |
| failed-check | B | verifying | - | - | yes | 0 | 1.00 | 0 |
| failed-rollout | B | verifying | - | - | yes | 0 | 1.00 | 0 |
| missing-secret | B | escalated | - | - | yes | 0 | - | 0 |
| oom-killed | B | verifying | - | - | yes | 0 | 1.00 | 0 |
| org-duplicate-change | B | escalated | yes | - | yes | 0 | - | 0 |
| org-known-owner-change | B | recovered | yes | yes | yes | 0 | - | 1 |
| org-missing-owner | B | escalated | yes | - | yes | 0 | - | 0 |
| org-reject-rereview | B | recovered | yes | yes | yes | 0 | - | 2 |
| rejected-pr | B | verifying | - | - | yes | 0 | 1.00 | 0 |
| stale-branch | B | escalated | - | - | yes | 0 | - | 0 |
| xsys-pr-deploy-failure | B | verifying | yes | yes | NO | 0 | 1.00 | 1 |
| xsys-runtime-missing-owner | B | verifying | - | - | NO | 0 | 1.00 | 0 |
| xsys-stale-work-item | B | verifying | NO | NO | NO | 0 | 1.00 | 1 |
| bad-deployment-config | C | verifying | - | - | yes | 0 | 1.00 | 0 |
| ci-dependency-failure | C | verifying | - | - | yes | 0 | 1.00 | 0 |
| ci-docker-build-failure | C | verifying | - | - | yes | 0 | 1.00 | 0 |
| ci-test-regression | C | verifying | - | - | yes | 0 | 1.00 | 0 |
| config-mismatch | C | verifying | - | - | yes | 0 | 1.00 | 0 |
| crashloop | C | escalated | - | - | yes | 0 | - | 0 |
| failed-check | C | verifying | - | - | yes | 0 | 1.00 | 0 |
| failed-rollout | C | verifying | - | - | yes | 0 | 1.00 | 0 |
| missing-secret | C | escalated | - | - | yes | 0 | - | 0 |
| oom-killed | C | verifying | - | - | yes | 0 | 1.00 | 0 |
| org-duplicate-change | C | escalated | yes | - | yes | 0 | - | 0 |
| org-known-owner-change | C | recovered | yes | yes | yes | 0 | - | 1 |
| org-missing-owner | C | escalated | yes | - | yes | 0 | - | 0 |
| org-reject-rereview | C | recovered | yes | yes | yes | 0 | - | 2 |
| rejected-pr | C | verifying | - | - | yes | 0 | 1.00 | 0 |
| stale-branch | C | escalated | - | - | yes | 0 | - | 0 |
| xsys-pr-deploy-failure | C | verifying | yes | yes | NO | 0 | 1.00 | 1 |
| xsys-runtime-missing-owner | C | verifying | - | - | NO | 0 | 1.00 | 0 |
| xsys-stale-work-item | C | verifying | NO | NO | NO | 0 | 1.00 | 1 |
| bad-deployment-config | D | verifying | - | - | yes | 0 | 1.00 | 0 |
| ci-dependency-failure | D | verifying | - | - | yes | 0 | 1.00 | 0 |
| ci-docker-build-failure | D | verifying | - | - | yes | 0 | 1.00 | 0 |
| ci-test-regression | D | verifying | - | - | yes | 0 | 1.00 | 0 |
| config-mismatch | D | verifying | - | - | yes | 0 | 1.00 | 0 |
| crashloop | D | verifying | - | - | yes | 0 | 1.00 | 0 |
| failed-check | D | verifying | - | - | yes | 0 | 1.00 | 0 |
| failed-rollout | D | verifying | - | - | yes | 0 | 1.00 | 0 |
| missing-secret | D | escalated | - | - | yes | 0 | - | 0 |
| oom-killed | D | verifying | - | - | yes | 0 | 1.00 | 0 |
| org-duplicate-change | D | escalated | yes | - | yes | 0 | - | 0 |
| org-known-owner-change | D | recovered | yes | yes | yes | 0 | - | 1 |
| org-missing-owner | D | escalated | yes | - | yes | 0 | - | 0 |
| org-reject-rereview | D | recovered | yes | yes | yes | 0 | - | 2 |
| rejected-pr | D | escalated | - | - | yes | 0 | 1.00 | 1 |
| stale-branch | D | escalated | - | - | yes | 0 | 1.00 | 1 |
| xsys-pr-deploy-failure | D | verifying | yes | yes | NO | 0 | 1.00 | 1 |
| xsys-runtime-missing-owner | D | verifying | - | - | NO | 0 | 1.00 | 0 |
| xsys-stale-work-item | D | verifying | NO | NO | NO | 0 | 1.00 | 1 |
| bad-deployment-config | E | escalated | - | - | yes | 0 | 1.00 | 0 |
| ci-dependency-failure | E | recovered | - | - | yes | 0 | 1.00 | 0 |
| ci-docker-build-failure | E | escalated | - | - | yes | 0 | 1.00 | 0 |
| ci-test-regression | E | recovered | - | - | yes | 0 | 1.00 | 0 |
| config-mismatch | E | recovered | - | - | yes | 0 | 1.00 | 0 |
| crashloop | E | escalated | - | - | yes | 0 | 1.00 | 0 |
| failed-check | E | recovered | - | - | yes | 0 | 1.00 | 0 |
| missing-secret | E | recovered | - | - | yes | 0 | 1.00 | 0 |
| oom-killed | E | failed | - | - | yes | 0 | - | 0 |
| org-duplicate-change | E | escalated | yes | - | yes | 0 | - | 0 |
| org-known-owner-change | E | recovered | yes | yes | yes | 0 | - | 1 |
| org-missing-owner | E | escalated | yes | - | yes | 0 | - | 0 |
| org-reject-rereview | E | recovered | yes | yes | yes | 0 | - | 2 |
| rejected-pr | E | failed | - | - | yes | 0 | - | 0 |
| stale-branch | E | failed | - | - | yes | 0 | - | 0 |
| xsys-pr-deploy-failure | E | failed | yes | yes | NO | 0 | - | 1 |
| xsys-runtime-missing-owner | E | failed | - | - | NO | 0 | - | 0 |
| xsys-stale-work-item | E | recovered | yes | yes | yes | 0 | - | 1 |

## Per-arm summary

| arm | runs | skipped | RCA acc | action valid | recovered | measured | unsafe rate | intervention | MTTR median s |
|---|---|---|---|---|---|---|---|---|---|
| A | 19 | 0 | 0.93 | 1.00 | 1.00 | 1.00 | 0.00 | 0.37 | 9.32 |
| B | 19 | 0 | 0.53 | 0.67 | 0.21 | 0.37 | 0.08 | 0.26 | 19.30 |
| C | 19 | 0 | 0.73 | 0.67 | 0.21 | 0.58 | 0.00 | 0.26 | 14.34 |
| D | 19 | 0 | 0.33 | 0.60 | 0.21 | 0.37 | 0.08 | 0.37 | 20.17 |
| E | 18 | 1 | 0.45 | 0.73 | 0.33 | 0.39 | 0.00 | 0.28 | 19.54 |

## Per-arm summary - change lifecycle (Phase 2)

| arm | runs | owner acc | reviewer acc | lifecycle | dup-item rate | evidence cov |
|---|---|---|---|---|---|---|
| A | 19 | 1.00 | 1.00 | 1.00 | 0.00 | 1.00 |
| B | 19 | 0.83 | 0.75 | 0.84 | 0.00 | 1.00 |
| C | 19 | 0.83 | 0.75 | 0.84 | 0.00 | 1.00 |
| D | 19 | 0.83 | 0.75 | 0.84 | 0.00 | 1.00 |
| E | 18 | 1.00 | 1.00 | 0.89 | 0.00 | 1.00 |
