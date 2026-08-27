#!/usr/bin/env bash
# Fault: missing-secret.
#
# Trigger: delete the Secret object, then restart the deployment so a new
# pod actually tries to start without it (an already-running pod keeps its
# env until it restarts).
#
# Measured on a real kind cluster: this does NOT produce CrashLoopBackOff.
# envFrom.secretRef points at a Secret that does not exist, so the kubelet
# can never resolve the pod's environment and never starts the container at
# all. The signal is containerStatuses[].state.waiting.reason ==
# "CreateContainerConfigError", naming the missing secret, with the
# container's restart count staying at 0 (it never runs, so it never exits
# and never accumulates restarts).
#
# This is also what distinguishes missing-secret from oom-killed (fault 3):
# OOMKilled has a terminated.reason/exitCode because the container did run
# and then got killed; missing-secret never reaches a running state at all.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../common.sh"

POLL_TIMEOUT=60

echo "[missing-secret] deleting Secret payment-api-secret"
kctl -n "${NAMESPACE}" delete secret payment-api-secret

echo "[missing-secret] restarting deployment/payment-api so a new pod tries to start without it"
kctl -n "${NAMESPACE}" rollout restart deployment/payment-api

echo "[missing-secret] waiting for a pod to hit CreateContainerConfigError"
found=""
for ((i = 0; i < POLL_TIMEOUT; i += 3)); do
  found="$(kctl -n "${NAMESPACE}" get pods -l app=payment-api -o json | python3 -c '
import json, sys
d = json.load(sys.stdin)
for item in d["items"]:
    for cs in item.get("status", {}).get("containerStatuses", []):
        waiting = cs.get("state", {}).get("waiting", {})
        if waiting.get("reason") == "CreateContainerConfigError":
            name = item["metadata"]["name"]
            print(name + " reason=" + str(waiting.get("reason")) + " restarts=" + str(cs.get("restartCount")) + " message=" + str(waiting.get("message")))
')"
  [[ -n "${found}" ]] && break
  sleep 3
done

if [[ -z "${found}" ]]; then
  echo "[missing-secret] FAIL: no pod reached CreateContainerConfigError within ${POLL_TIMEOUT}s" >&2
  exit 1
fi
echo "[missing-secret] evidence: ${found}"

if ! grep -q "payment-api-secret" <<<"${found}"; then
  echo "[missing-secret] FAIL: waiting message did not name the missing secret" >&2
  exit 1
fi

if ! grep -q "restarts=0" <<<"${found}"; then
  echo "[missing-secret] FAIL: expected restart count 0 (container never started)" >&2
  exit 1
fi

echo "[missing-secret] PASS"
