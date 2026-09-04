#!/usr/bin/env bash
# Fault: bad-deployment-config.
#
# Trigger: patch the deployment's memory limit to a value that is not a
# valid Kubernetes quantity ("not-a-quantity").
#
# Measured on a real kind cluster: this is rejected synchronously by the API
# server's schema validation. The `kubectl patch` call itself fails - no new
# pod, no new ReplicaSet, no rollout is ever attempted. This is what makes
# bad-deployment-config distinct from every other fault in the catalogue:
# those all get past admission and fail later (readiness, image pull, OOM,
# missing secret); this one never gets applied at all. Not timing-dependent,
# so no poll loop is needed - the rejection is synchronous.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../common.sh"

echo "[bad-deployment-config] patching memory limit to an invalid quantity"
set +e
patch_output="$(kctl -n "${NAMESPACE}" patch deployment payment-api --type=json \
  -p '[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"not-a-quantity"}]' 2>&1)"
patch_exit=$?
set -e

echo "${patch_output}"

if [[ "${patch_exit}" -eq 0 ]]; then
  echo "[bad-deployment-config] FAIL: patch was accepted instead of rejected - fault did not fire" >&2
  exit 1
fi

if ! grep -q "quantities must match" <<<"${patch_output}"; then
  echo "[bad-deployment-config] FAIL: rejection did not name the expected quantity validation error" >&2
  exit 1
fi
echo "[bad-deployment-config] evidence: patch rejected (exit ${patch_exit})"

current_limit="$(kctl -n "${NAMESPACE}" get deployment payment-api \
  -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}')"

if [[ "${current_limit}" != "128Mi" ]]; then
  echo "[bad-deployment-config] FAIL: memory limit changed to ${current_limit}, expected it to stay 128Mi" >&2
  exit 1
fi
echo "[bad-deployment-config] evidence: memory limit unchanged (${current_limit})"

echo "[bad-deployment-config] PASS"
