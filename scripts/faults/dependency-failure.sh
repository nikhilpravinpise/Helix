#!/usr/bin/env bash
# Fault: dependency-failure.
#
# Trigger: scale downstream-stub to zero replicas. Config stays correct - the
# dependency itself vanishes. This is what keeps dependency-failure distinct
# from config-mismatch (bad URL, healthy dependency): see
# sandbox/payment-api/k8s/01-downstream-stub.yaml.
#
# Signal: downstream-stub has zero pods AND payment-api's /ready returns 503.
# Both are asserted, within a bounded poll, before exiting 0.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../common.sh"

STUB_TIMEOUT=60
READY_TIMEOUT=60

echo "[dependency-failure] scaling downstream-stub to 0 replicas"
kctl -n "${NAMESPACE}" scale deployment/downstream-stub --replicas=0

echo "[dependency-failure] waiting for downstream-stub pods to reach 0"
stub_pods=""
for ((i = 0; i < STUB_TIMEOUT; i += 2)); do
  stub_pods="$(kctl -n "${NAMESPACE}" get pods -l app=downstream-stub --no-headers 2>/dev/null | wc -l)"
  [[ "${stub_pods}" -eq 0 ]] && break
  sleep 2
done

if [[ "${stub_pods}" -ne 0 ]]; then
  echo "[dependency-failure] FAIL: downstream-stub still has ${stub_pods} pod(s) after ${STUB_TIMEOUT}s" >&2
  exit 1
fi
echo "[dependency-failure] evidence: downstream-stub pods = 0"

PF_LOG="$(mktemp)"
cleanup() {
  if [[ -n "${PF_PID:-}" ]]; then
    kill "${PF_PID}" 2>/dev/null || true
    wait "${PF_PID}" 2>/dev/null || true
  fi
  rm -f "${PF_LOG}"
}
trap cleanup EXIT

# kubectl invoked directly, not through kctl: backgrounding a shell function
# makes $! the subshell's pid, orphaning the real kubectl process on kill.
kubectl --context "${KUBE_CONTEXT}" -n "${NAMESPACE}" \
  port-forward svc/payment-api :8000 >"${PF_LOG}" 2>&1 &
PF_PID=$!

LOCAL_PORT=""
for ((i = 0; i < 30; i++)); do
  LOCAL_PORT="$(sed -n 's/.*127\.0\.0\.1:\([0-9]\{1,\}\).*/\1/p' "${PF_LOG}" | head -1)"
  [[ -n "${LOCAL_PORT}" ]] && break
  sleep 1
done

if [[ -z "${LOCAL_PORT}" ]]; then
  echo "[dependency-failure] FAIL: port-forward never reported a local port" >&2
  exit 1
fi

status=""
for ((i = 0; i < READY_TIMEOUT / 2; i++)); do
  status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://localhost:${LOCAL_PORT}/ready" || true)"
  [[ "${status}" == "503" ]] && break
  sleep 2
done

if [[ "${status}" != "503" ]]; then
  echo "[dependency-failure] FAIL: /ready never returned 503 (last status: ${status:-none})" >&2
  exit 1
fi

echo "[dependency-failure] evidence: /ready returned 503"
echo "[dependency-failure] PASS"
