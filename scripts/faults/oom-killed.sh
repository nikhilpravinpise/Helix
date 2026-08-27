#!/usr/bin/env bash
# Fault: oom-killed.
#
# Trigger: set PAYMENT_API_BALLAST_MB=200 against the deployment's 128Mi
# memory limit (see sandbox/payment-api/k8s/04-deployment.yaml). The app
# allocates the ballast at startup, which blows the limit.
#
# Signal: a payment-api pod's containerStatuses[].lastState.terminated
# reports reason == "OOMKilled" and exitCode == 137, and the pod settles into
# CrashLoopBackOff. Distinguishing detail vs. missing-secret (fault 4): that
# fault never starts the container at all (CreateContainerConfigError, no
# exit code), so terminated.reason/exitCode never populate for it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../common.sh"

POLL_TIMEOUT=90

echo "[oom-killed] setting PAYMENT_API_BALLAST_MB=200 (limit is 128Mi)"
kctl -n "${NAMESPACE}" set env deployment/payment-api PAYMENT_API_BALLAST_MB=200

echo "[oom-killed] waiting for a pod to be OOMKilled"
found=""
for ((i = 0; i < POLL_TIMEOUT; i += 3)); do
  found="$(kctl -n "${NAMESPACE}" get pods -l app=payment-api -o json | python3 -c '
import json, sys
d = json.load(sys.stdin)
for item in d["items"]:
    for cs in item.get("status", {}).get("containerStatuses", []):
        term = cs.get("lastState", {}).get("terminated", {})
        if term.get("reason") == "OOMKilled":
            name = item["metadata"]["name"]
            print(name + " reason=" + str(term.get("reason")) + " exitCode=" + str(term.get("exitCode")))
')"
  [[ -n "${found}" ]] && break
  sleep 3
done

if [[ -z "${found}" ]]; then
  echo "[oom-killed] FAIL: no pod reached OOMKilled within ${POLL_TIMEOUT}s" >&2
  exit 1
fi
echo "[oom-killed] evidence: ${found}"

if ! grep -q "exitCode=137" <<<"${found}"; then
  echo "[oom-killed] FAIL: OOMKilled pod did not report exitCode 137" >&2
  exit 1
fi

echo "[oom-killed] waiting for CrashLoopBackOff"
crashloop=""
for ((i = 0; i < POLL_TIMEOUT; i += 3)); do
  crashloop="$(kctl -n "${NAMESPACE}" get pods -l app=payment-api -o jsonpath='{range .items[*]}{.status.containerStatuses[0].state.waiting.reason}{"\n"}{end}' | grep -c '^CrashLoopBackOff$' || true)"
  [[ "${crashloop}" -gt 0 ]] && break
  sleep 3
done

if [[ "${crashloop}" -eq 0 ]]; then
  echo "[oom-killed] FAIL: no pod reached CrashLoopBackOff within ${POLL_TIMEOUT}s" >&2
  exit 1
fi

echo "[oom-killed] evidence: pod is in CrashLoopBackOff"

echo "[oom-killed] PASS"
