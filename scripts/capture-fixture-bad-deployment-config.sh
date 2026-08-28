#!/usr/bin/env bash
# One-off capture for bad-deployment-config (ticket #7). Not part of the
# committed fault/bring-up/teardown scripts.
#
# Every other cluster fault gets past API server admission and fails later
# (readiness, image pull, OOM, missing secret), so its evidence is a pod
# status or a Kubernetes event. bad-deployment-config is rejected
# SYNCHRONOUSLY by the API server's schema validation - see
# scripts/faults/bad-deployment-config.sh. No new pod, no new ReplicaSet, no
# event is ever generated, so there is nothing for kubectl get
# pods/events or Elasticsearch to show. Capturing this the same way as the
# other six faults would produce an empty, misleading fixture. Instead this
# captures the apply/patch attempt itself (stdout+stderr+exit code) plus the
# deployment's resource limit before and after, which is where this fault's
# evidence actually lives.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${SCRIPT_DIR}/common.sh"

FAULT="bad-deployment-config"
OUT_DIR="${REPO_ROOT}/fixtures/faults/cluster"
mkdir -p "${OUT_DIR}"
OUT_FILE="${OUT_DIR}/${FAULT}.json"

echo "===== ${FAULT}: reset before ====="
"${SCRIPT_DIR}/reset.sh"

LIMIT_BEFORE="$(kctl -n "${NAMESPACE}" get deployment payment-api \
  -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}')"

echo "===== ${FAULT}: trigger ====="
FAULT_LOG="$(mktemp)"
set +e
"${SCRIPT_DIR}/faults/${FAULT}.sh" >"${FAULT_LOG}" 2>&1
FAULT_EXIT=$?
set -e
cat "${FAULT_LOG}"

LIMIT_AFTER="$(kctl -n "${NAMESPACE}" get deployment payment-api \
  -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}')"

# Confirm, independently of the fault script's own assertion, that nothing
# cluster-side fired: no pod events attributable to this attempt.
PODS_FILE="$(mktemp)"
kctl -n "${NAMESPACE}" get pods -l app=payment-api -o json >"${PODS_FILE}"

CAPTURED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

python3 - "${OUT_FILE}" "${CAPTURED_AT}" "${FAULT_EXIT}" "${FAULT_LOG}" "${LIMIT_BEFORE}" "${LIMIT_AFTER}" "${PODS_FILE}" <<'PYEOF'
import json
import sys

out_file, captured_at, fault_exit, fault_log_path, limit_before, limit_after, pods_path = sys.argv[1:8]

with open(fault_log_path) as f:
    fault_output = f.read()
with open(pods_path) as f:
    pods = json.load(f)

fixture = {
    "fault": "bad-deployment-config",
    "layer": "cluster",
    "note": (
        "This fault is rejected synchronously by the API server's schema "
        "validation before admission. No pod, no ReplicaSet, no Kubernetes "
        "event, and no application log line is ever produced - the "
        "evidence lives entirely at the apply/kubectl layer, captured "
        "below as fault_script_output. pods_payment_api_after is included "
        "only to show it is byte-for-byte the pre-fault state (no new pod "
        "created)."
    ),
    "captured_at": captured_at,
    "trigger": (
        "kubectl patch deployment/payment-api --type=json, replacing "
        "spec.template.spec.containers[0].resources.limits.memory with the "
        "invalid quantity string \"not-a-quantity\""
    ),
    "fault_script": "faults/bad-deployment-config.sh",
    "fault_script_exit_code": int(fault_exit),
    "fault_script_output": fault_output,
    "evidence": {
        "memory_limit_before_patch_attempt": limit_before,
        "memory_limit_after_patch_attempt": limit_after,
        "pods_payment_api_after": pods,
    },
}

with open(out_file, "w") as f:
    json.dump(fixture, f, indent=2)
    f.write("\n")

print(f"wrote {out_file}")
PYEOF

rm -f "${FAULT_LOG}" "${PODS_FILE}"

echo "===== ${FAULT}: reset after ====="
"${SCRIPT_DIR}/reset.sh"

echo "===== ${FAULT}: fixture written to ${OUT_FILE} ====="
