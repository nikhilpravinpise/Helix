#!/usr/bin/env bash
# Subphase E7 correlation accuracy, against real ground truth.
#
# events/metrics.py::correlation_accuracy needs expected_groups (which
# event ids should land on one incident) - there is no way to derive that
# from the store itself. benchmark/correlation/groups.py declares it from
# real fixtures/faults/ evidence run through the real adapters; this
# script ingests those events through events.ingest.ingest_event and
# reports the accuracy against them.
#
#   bash scripts/correlation-accuracy.sh          # human-readable
#   bash scripts/correlation-accuracy.sh --json   # JSON
#
# Needs the host Postgres from infra/postgres-compose.yml (container
# helix-postgres) reachable at HELIX_DATABASE_URL. Idempotent - safe to
# run more than once against the same database.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

exec uv run python -m benchmark.correlation.run "$@"
