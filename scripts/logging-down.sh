#!/usr/bin/env bash
# Tear down the host-side log store (Elasticsearch and Kibana) entirely,
# including its volumes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/docker-compose.yml"

docker compose -f "${COMPOSE_FILE}" down --volumes
