#!/usr/bin/env bash
# Bring up the kind sandbox: cluster, images, manifests, wait for Available.
# Idempotent - safe to run again against an already-up sandbox.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

if kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
  echo "kind cluster ${CLUSTER_NAME} already exists, skipping create"
else
  kind create cluster --name "${CLUSTER_NAME}"
fi

docker build --provenance=false -t "${IMAGE_TAG}" "${K8S_DIR}/../"

docker pull --platform linux/amd64 "${DOWNSTREAM_IMAGE}"

# `kind load docker-image` shells out to `docker save` without a --platform
# filter, which chokes on multi-arch images (docker's containerd-backed
# store only has local content for the pulled platform, but the exported
# manifest list still lists the others). Load directly with a single-
# platform save instead, mirroring what kind does internally.
NODE="$(kind get nodes --name "${CLUSTER_NAME}" | head -1)"
for image in "${IMAGE_TAG}" "${DOWNSTREAM_IMAGE}"; do
  docker save --platform linux/amd64 "${image}" \
    | docker exec -i "${NODE}" ctr --namespace=k8s.io images import --digests --snapshotter=overlayfs -
done

kctl apply -f "${K8S_DIR}"

kctl -n "${NAMESPACE}" wait deployment/downstream-stub --for=condition=Available --timeout=120s
kctl -n "${NAMESPACE}" wait deployment/payment-api --for=condition=Available --timeout=120s

echo "sandbox up: cluster ${CLUSTER_NAME}, namespace ${NAMESPACE}"
