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

# Wait until the admission webhook can actually answer, not for the
# Deployment's Available condition: a freshly created Deployment can
# report Available while its pod is still ContainerCreating, and the next
# Ingress apply then fails on the webhook with "connection refused".
# Two things must hold. The controller pod, which serves the webhook, is
# Ready - polled into existence first, since `wait pod --selector` errors
# on zero matches. And the webhook carries its CA bundle, which the
# admission-patch job writes; waiting on that job directly is a race,
# because ttlSecondsAfterFinished: 0 deletes it the moment it finishes.
for _ in $(seq 90); do
  kctl -n ingress-nginx get pod --selector=app.kubernetes.io/component=controller \
    -o name 2>/dev/null | grep -q . && break
  sleep 2
done
kctl -n ingress-nginx wait pod --selector=app.kubernetes.io/component=controller \
  --for=condition=Ready --timeout=180s
for _ in $(seq 90); do
  kctl get validatingwebhookconfiguration ingress-nginx-admission \
    -o jsonpath='{.webhooks[0].clientConfig.caBundle}' 2>/dev/null | grep -q . && break
  sleep 2
done

echo "ingress-nginx up: host :80 -> node :30080, host :443 -> node :30443"
