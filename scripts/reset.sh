#!/usr/bin/env bash
# Return the payment-api sandbox to known-good without recreating the cluster.
# Fast path between benchmark scenarios (1.11): reapply manifests, clear any
# `kubectl set env` overrides, scale downstream-stub back to 1, then VERIFY -
# both deployments' rollouts must genuinely complete and /ready must answer
# 200 - before exiting 0.
#
# Why rollout status and not just `wait --for=condition=Available`: a bad
# PAYMENT_API_DOWNSTREAM_URL pushed via `kubectl set env` does not take the
# service down - the new pod fails readiness, so Kubernetes keeps the old pod
# serving and the Deployment can stay "Available" while the rollout is
# wedged. `rollout status` blocks until the new replica set is actually the
# one serving, which is what genuinely detects the wedge.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

ROLLOUT_TIMEOUT=120s
READY_MAX_ATTEMPTS=30

kctl apply -f "${K8S_DIR}"

# Drop every `kubectl set env` override in one shot rather than naming each
# variable. The manifests supply configuration through envFrom, so the
# container's own env list is only ever populated by a fault, and clearing it
# wholesale returns to manifest state.
#
# Naming variables individually does not work: `kubectl apply` never owned a
# field that only ever arrived via `set env`, so its three-way merge has no
# record of it and leaves it in place. PAYMENT_API_BALLAST_MB survived reset
# that way and left the sandbox crashlooping. Enumerating known variables
# would also mean every new fault silently depends on someone remembering to
# extend this list, and a fault script that is interrupted before its own
# cleanup would leave a sandbox reset.sh cannot repair.
#
# Strategic merge, not --type=merge. A JSON merge patch replaces the whole
# containers array rather than merging into it, which drops required fields
# like image and the API server rejects the patch.
kctl -n "${NAMESPACE}" patch deployment/payment-api \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"payment-api","env":null}]}}}}' >/dev/null

kctl -n "${NAMESPACE}" scale deployment/downstream-stub --replicas=1

kctl -n "${NAMESPACE}" rollout status deployment/downstream-stub --timeout="${ROLLOUT_TIMEOUT}"
kctl -n "${NAMESPACE}" rollout status deployment/payment-api --timeout="${ROLLOUT_TIMEOUT}"

PF_LOG="$(mktemp)"

cleanup() {
  if [[ -n "${PF_PID:-}" ]]; then
    kill "${PF_PID}" 2>/dev/null || true
    wait "${PF_PID}" 2>/dev/null || true
  fi
  rm -f "${PF_LOG}"
}
trap cleanup EXIT

# Let kubectl pick a free local port rather than pinning one. A pinned port
# turns any leaked forward from an earlier run into a silent false result:
# this forward fails to bind, and the curl below reaches the stale forward
# instead, so reset.sh reports on a connection it does not own. Between every
# benchmark scenario in 1.11, that is a flake that would read as an agent
# failure rather than a harness one.
#
# kubectl is invoked directly here rather than through the kctl helper:
# backgrounding a shell function runs it in a subshell, so $! would be the
# subshell's pid, and killing that orphans the real kubectl process, which
# keeps running and holding its port.
kubectl --context "${KUBE_CONTEXT}" -n "${NAMESPACE}" \
  port-forward svc/payment-api :8000 >"${PF_LOG}" 2>&1 &
PF_PID=$!

READY_PORT=""
for ((i = 0; i < 30; i++)); do
  READY_PORT="$(sed -n 's/.*127\.0\.0\.1:\([0-9]\{1,\}\).*/\1/p' "${PF_LOG}" | head -1)"
  [[ -n "${READY_PORT}" ]] && break
  sleep 1
done

if [[ -z "${READY_PORT}" ]]; then
  echo "reset failed: port-forward never reported a local port" >&2
  exit 1
fi

status=""
for ((i = 0; i < READY_MAX_ATTEMPTS; i++)); do
  status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://localhost:${READY_PORT}/ready" || true)"
  if [[ "${status}" == "200" ]]; then
    break
  fi
  sleep 1
done

if [[ "${status}" != "200" ]]; then
  echo "reset failed: /ready did not return 200 (last status: ${status:-none})" >&2
  exit 1
fi

echo "sandbox reset to known-good: /ready is 200"
