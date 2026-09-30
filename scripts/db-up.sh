#!/usr/bin/env bash
# Bring up the host-side Postgres for Helix incident state
# (infra/postgres-compose.yml, decision D3). Waits for the healthcheck so
# callers can migrate or intake immediately. Idempotent - safe to re-run.
#
# After first bring-up, apply the schema with:
#   uv run alembic -c state/migrations/alembic.ini upgrade head
# (or `make db-migrate`).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/postgres-compose.yml"

# POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB override the helix/helix/helix
# defaults; a repo-root .env is picked up when present.
ENV_ARGS=()
if [[ -f "${REPO_ROOT}/.env" ]]; then
  ENV_ARGS+=(--env-file "${REPO_ROOT}/.env")
fi

docker compose "${ENV_ARGS[@]}" -f "${COMPOSE_FILE}" up -d --wait

echo "postgres up: localhost:5432 (db: ${POSTGRES_DB:-helix})"
