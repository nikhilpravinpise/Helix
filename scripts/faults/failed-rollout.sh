#!/usr/bin/env bash
# Fault: failed-rollout.
#
# Trigger: point the deployment at an image tag that does not exist.
#
# Signal: the new pod oscillates between waiting reasons "ErrImagePull" and
# "ImagePullBackOff" (measured on a real kind cluster - kubelet toggles
# between the two on each retry), and the rollout wedges: old ReplicaSet
# stays ready=1, new ReplicaSet stays ready=0, and `rollout status --timeout`
# times out. Same wedge shape as config-mismatch, but distinguished by the
# image-pull reason on the new pod rather than a readiness probe failure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../common.sh"

POLL_TIMEOUT=150
ROLLOUT_TIMEOUT=120s

echo "[failed-rollout] setting payment-api image to a nonexistent tag"
kctl -n "${NAMESPACE}" set image deployment/payment-api payment-api=payment-api:doesnotexist

echo "[failed-rollout] waiting for an image-pull failure reason on the new pod"
found=""
for ((i = 0; i < POLL_TIMEOUT; i += 3)); do
  found="$(kctl -n "${NAMESPACE}" get pods -l app=payment-api -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.containerStatuses[0].state.waiting.reason}{"\n"}{end}' | awk '$2 == "ErrImagePull" || $2 == "ImagePullBackOff"')"
  [[ -n "${found}" ]] && break
  sleep 3
done

if [[ -z "${found}" ]]; then
  echo "[failed-rollout] FAIL: no pod reached ErrImagePull/ImagePullBackOff within ${POLL_TIMEOUT}s" >&2
  exit 1
fi
echo "[failed-rollout] evidence: ${found}"

echo "[failed-rollout] confirming the rollout is wedged"
set +e
kctl -n "${NAMESPACE}" rollout status deployment/payment-api --timeout="${ROLLOUT_TIMEOUT}"
rollout_exit=$?
set -e

if [[ "${rollout_exit}" -eq 0 ]]; then
  echo "[failed-rollout] FAIL: rollout completed instead of wedging - fault did not fire" >&2
  exit 1
fi
echo "[failed-rollout] evidence: rollout status timed out (exit ${rollout_exit})"

echo "[failed-rollout] PASS"
