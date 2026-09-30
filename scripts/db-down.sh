#!/usr/bin/env bash
# Stop the host-side Postgres for Helix incident state. The named volume is
# kept so incident data survives restarts; add --volumes (or run
# `docker compose -f infra/postgres-compose.yml down --volumes`) to wipe it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/postgres-compose.yml"

docker compose -f "${COMPOSE_FILE}" down
