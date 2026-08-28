#!/usr/bin/env bash
# Bring up the host-side Jenkins for the payment-api pipeline (ticket #5).
# Idempotent - safe to run again against an already-up Jenkins.
#
# Requires the kind cluster to already exist (`bash scripts/up.sh`): this
# service attaches to the "kind" Docker network that kind creates, and the
# pipeline's deploy stage talks to that cluster. See
# infra/jenkins-compose.yml for why.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/jenkins-compose.yml"

JENKINS_URL="http://localhost:8080"
READY_TIMEOUT=180

if ! docker network inspect kind >/dev/null 2>&1; then
  echo "jenkins-up failed: the 'kind' Docker network does not exist yet - run 'bash scripts/up.sh' first" >&2
  exit 1
fi

docker compose -f "${COMPOSE_FILE}" up -d --build

echo "waiting for jenkins to come up (timeout ${READY_TIMEOUT}s)"
status=""
for ((i = 0; i < READY_TIMEOUT; i += 3)); do
  status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "${JENKINS_URL}/login" || true)"
  if [[ "${status}" == "200" ]]; then
    break
  fi
  sleep 3
done

if [[ "${status}" != "200" ]]; then
  echo "jenkins-up failed: jenkins did not come up within ${READY_TIMEOUT}s (last status: ${status:-none})" >&2
  exit 1
fi

echo "jenkins up: ${JENKINS_URL} (admin / admin-sandbox-placeholder)"
