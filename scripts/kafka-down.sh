#!/usr/bin/env bash
# Stop the Kafka backbone. The named volume is kept so the event log and
# consumer offsets survive a restart - that durability is an E2 exit
# criterion, so wiping by default would be wrong. Add --volumes (or run
# `docker compose -f infra/kafka-compose.yml down --volumes`) to clear it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/kafka-compose.yml"

docker compose -f "${COMPOSE_FILE}" down "$@"
