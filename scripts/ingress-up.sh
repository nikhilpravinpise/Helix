#!/usr/bin/env bash
# Install the ingress-nginx controller into the kind sandbox and wait for
# it (subphase 1.13). Idempotent - safe to re-run. Without this the
# payment-api Ingress object applies cleanly but routes nothing: the
# cluster has no ingress controller otherwise.
#
# The manifest is the pinned upstream kind provider deploy (controller
# v1.14.1) with its Service patched to NodePort 30080/30443 to match
# infra/kind-config.yaml's extraPortMappings - see the header of
# sandbox/payment-api/k8s/ingress-nginx/deploy.yaml.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

kctl apply -f "$(winpath "${K8S_DIR}/ingress-nginx/deploy.yaml")"

kctl -n ingress-nginx wait deployment/ingress-nginx-controller \
  --for=condition=Available --timeout=180s

echo "ingress-nginx up: host :80 -> node :30080, host :443 -> node :30443"
