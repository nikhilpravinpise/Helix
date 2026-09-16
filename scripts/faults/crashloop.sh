#!/usr/bin/env bash
# Fault: crashloop.
#
# Trigger: leave the Secret in place but set PAYMENT_API_API_KEY to
# whitespace, then restart the deployment.
#
# This is the only fault in the catalogue that exercises payment-api's own
# fail-fast startup validation. Deleting the Secret (missing-secret.sh) never
# gets that far: the kubelet cannot resolve envFrom, so the container never
# starts and the application code never runs. Here the Secret resolves, the
# container starts, the config validator rejects the empty value, and the
# process exits 1.
#
# Measured on a real kind cluster. Distinguishable from the two neighbouring
# faults by evidence, which is what matters for the 1.11 benchmark:
#
#   crashloop       CrashLoopBackOff, terminated.reason=Error,  exitCode=1
#   oom-killed      CrashLoopBackOff, terminated.reason=OOMKilled, exitCode=137
#   missing-secret  CreateContainerConfigError, never runs, restartCount=0
#
# reset.sh restores the correct value, since the Secret is manifest-owned and
# its apply reverts stringData.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../common.sh"

POLL_TIMEOUT=180

echo "[crashloop] setting PAYMENT_API_API_KEY to whitespace"
kctl -n "${NAMESPACE}" patch secret payment-api-secret \
  -p '{"stringData":{"PAYMENT_API_API_KEY":"   "}}' >/dev/null

echo "[crashloop] restarting deployment/payment-api"
kctl -n "${NAMESPACE}" rollout restart deployment/payment-api >/dev/null

echo "[crashloop] waiting for CrashLoopBackOff with a non-OOM exit"
found=""
for ((i = 0; i < POLL_TIMEOUT; i += 3)); do
  found="$(kctl -n "${NAMESPACE}" get pods -l app=payment-api -o json | python3 -c '
import json, sys
for pod in json.load(sys.stdin)["items"]:
    for cs in pod.get("status", {}).get("containerStatuses", []):
        waiting = cs.get("state", {}).get("waiting", {})
        term = cs.get("lastState", {}).get("terminated", {})
        if waiting.get("reason") == "CrashLoopBackOff" and term.get("reason") == "Error":
            print("%s reason=%s exitCode=%s restarts=%s" % (
                pod["metadata"]["name"], term.get("reason"),
                term.get("exitCode"), cs.get("restartCount")))
')"
  [[ -n "${found}" ]] && break
  sleep 3
done

if [[ -z "${found}" ]]; then
  echo "[crashloop] FAIL: no pod reached CrashLoopBackOff with reason=Error within ${POLL_TIMEOUT}s" >&2
  exit 1
fi
echo "[crashloop] evidence: ${found}"

# Assert on the exit code, not just on CrashLoopBackOff. Without this the
# fault is indistinguishable from oom-killed, and the Diagnosis Agent cannot
# be benchmarked on telling a bad config value from a memory limit.
if ! grep -q "exitCode=1 " <<<"${found} "; then
  echo "[crashloop] FAIL: expected exitCode 1, got: ${found}" >&2
  exit 1
fi

if grep -q "restarts=0" <<<"${found}"; then
  echo "[crashloop] FAIL: container never ran, that is the missing-secret signature" >&2
  exit 1
fi

echo "[crashloop] PASS"
