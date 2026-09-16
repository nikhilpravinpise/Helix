#!/usr/bin/env bash
# Fault: config-mismatch.
#
# Trigger: point PAYMENT_API_DOWNSTREAM_URL at a host that does not exist.
# The dependency itself stays healthy - only the config is wrong. This is
# what keeps config-mismatch distinct from dependency-failure.
#
# Measured on a real kind cluster: this does NOT degrade the service. The new
# pod fails readiness, so Kubernetes keeps the old pod serving and traffic
# stays 200. The Deployment still reports Available=True. The real signal is
# a wedged rollout: `kubectl rollout status --timeout` times out, with the
# new ReplicaSet stuck at ready=0 while the old ReplicaSet stays at ready=1.
# Asserting on a 503 or on Available=True would silently pass this fault
# without it ever having fired - so this script asserts on rollout state
# only.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../common.sh"

ROLLOUT_TIMEOUT=120s

echo "[config-mismatch] pointing PAYMENT_API_DOWNSTREAM_URL at a nonexistent host"
kctl -n "${NAMESPACE}" set env deployment/payment-api \
  PAYMENT_API_DOWNSTREAM_URL=http://downstream-does-not-exist.invalid

echo "[config-mismatch] waiting for the rollout to wedge (expecting a timeout)"
set +e
kctl -n "${NAMESPACE}" rollout status deployment/payment-api --timeout="${ROLLOUT_TIMEOUT}"
rollout_exit=$?
set -e

if [[ "${rollout_exit}" -eq 0 ]]; then
  echo "[config-mismatch] FAIL: rollout completed instead of wedging - fault did not fire" >&2
  exit 1
fi
echo "[config-mismatch] evidence: rollout status timed out (exit ${rollout_exit})"

# Old, scaled-down ReplicaSets from earlier rollouts stick around (bounded by
# revisionHistoryLimit), so picking "old" vs "new" by creation order among all
# ReplicaSets is wrong. Only the two ReplicaSets currently desired (replicas
# > 0) are the ones in the wedge: the old one still serving and the new one
# stuck at ready=0.
rs_summary="$(kctl -n "${NAMESPACE}" get rs -l app=payment-api -o json | python3 -c '
import json, sys
d = json.load(sys.stdin)
active = [rs for rs in d["items"] if rs["spec"].get("replicas", 0) > 0]
active.sort(key=lambda rs: rs["metadata"]["creationTimestamp"])
for rs in active:
    name = rs["metadata"]["name"]
    ready = rs["status"].get("readyReplicas", 0)
    print(name + " ready=" + str(ready))
')"

echo "[config-mismatch] evidence: active ReplicaSets:"
echo "${rs_summary}"

active_count="$(wc -l <<<"${rs_summary}")"
old_rs_ready="$(awk 'NR==1' <<<"${rs_summary}" | sed -n 's/.*ready=\([0-9]*\)/\1/p')"
new_rs_ready="$(awk 'NR==2' <<<"${rs_summary}" | sed -n 's/.*ready=\([0-9]*\)/\1/p')"

if [[ "${active_count}" -ne 2 || "${old_rs_ready:-0}" -ne 1 || "${new_rs_ready:-0}" -ne 0 ]]; then
  echo "[config-mismatch] FAIL: expected exactly 2 active ReplicaSets, old ready=1 and new ready=0" >&2
  exit 1
fi

echo "[config-mismatch] PASS"
