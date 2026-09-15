#!/usr/bin/env bash
# Subphase 1.11 controlled benchmark runner.
#
# Runs the benchmark harness through the real CLI paths and writes the
# report artifacts to benchmark/reports/.
#
#   bash scripts/benchmark.sh                 # arm A, all scenarios
#   bash scripts/benchmark.sh --arm A --arm E # multiple arms
#   bash scripts/benchmark.sh --scenario ci-test-regression
#   bash scripts/benchmark.sh --mode fixture  # force fixture evidence
#
# Live scenarios additionally need the sandbox stack (kind context
# kind-helix-sandbox, Elasticsearch, Jenkins); missing infrastructure
# marks the scenario skipped rather than failing the run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

exec uv run python -m benchmark.harness "$@"
