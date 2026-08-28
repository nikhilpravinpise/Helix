#!/usr/bin/env bash
# Tear down the host-side Jenkins (ticket #5) entirely, including its volume.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/jenkins-compose.yml"

docker compose -f "${COMPOSE_FILE}" down --volumes
