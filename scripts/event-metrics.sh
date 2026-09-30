#!/usr/bin/env bash
# Subphase E7 event-layer report.
#
# Prints correlation accuracy, duplicate handling rate, event latency, and
# replay correctness read from the live event store.
#
#   bash scripts/event-metrics.sh          # human-readable
#   bash scripts/event-metrics.sh --json   # JSON
#
# Needs the host Postgres from infra/postgres-compose.yml (container
# helix-postgres) reachable at HELIX_DATABASE_URL.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

exec uv run python -m events.metrics "$@"
