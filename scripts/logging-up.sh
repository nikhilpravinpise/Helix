#!/usr/bin/env bash
# Bring up the host-side log store: Elasticsearch and Kibana in Docker,
# outside the kind cluster. Idempotent - safe to run again against an
# already-up stack.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/docker-compose.yml"

ES_URL="http://localhost:9200"
HEALTH_TIMEOUT=120

docker compose -f "${COMPOSE_FILE}" up -d

echo "waiting for elasticsearch to report healthy (timeout ${HEALTH_TIMEOUT}s)"
status=""
for ((i = 0; i < HEALTH_TIMEOUT; i += 3)); do
  status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "${ES_URL}/_cluster/health" || true)"
  if [[ "${status}" == "200" ]]; then
    break
  fi
  sleep 3
done

if [[ "${status}" != "200" ]]; then
  echo "logging-up failed: elasticsearch did not become healthy within ${HEALTH_TIMEOUT}s (last status: ${status:-none})" >&2
  exit 1
fi

echo "logging stack up: elasticsearch at ${ES_URL}, kibana at http://localhost:5601"
