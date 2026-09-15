#!/usr/bin/env bash
# Bring up the kind sandbox: cluster (with the ingress port mappings from
# infra/kind-config.yaml), images, ingress-nginx controller, manifests,
# wait for Available. Idempotent - safe to run again against an already-up
# sandbox.
#
# Cluster config note: a cluster created BEFORE infra/kind-config.yaml
# existed (plain `kind create cluster`) has no port mappings, so
# payment-api.local can never route - delete and recreate it once:
#   kind delete cluster --name helix-sandbox && bash scripts/up.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${SCRIPT_DIR}/common.sh"

if kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
  echo "kind cluster ${CLUSTER_NAME} already exists, skipping create"
else
  kind create cluster --config "$(winpath "${REPO_ROOT}/infra/kind-config.yaml")"
fi

"${SCRIPT_DIR}/load-sandbox-images.sh"

"${SCRIPT_DIR}/ingress-up.sh"

kctl apply -f "$(winpath "${K8S_DIR}")"
kctl apply -f "$(winpath "${REPO_ROOT}/infra/helix-ingress.yaml")"

kctl -n "${NAMESPACE}" wait deployment/downstream-stub --for=condition=Available --timeout=120s
kctl -n "${NAMESPACE}" wait deployment/payment-api --for=condition=Available --timeout=120s

echo "sandbox up: cluster ${CLUSTER_NAME}, namespace ${NAMESPACE}"
echo "ingress: http://payment-api.local (needs 127.0.0.1 payment-api.local in the hosts file)"
