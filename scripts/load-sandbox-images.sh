#!/usr/bin/env bash
# Build payment-api:kind and load it (plus the downstream stub image) into
# the kind cluster's containerd. Shared by scripts/up.sh and the Terraform
# images step (infra/terraform) so both bring-up paths load images exactly
# the same way. Idempotent - safe to re-run against an already-loaded node.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

docker build --provenance=false -t "${IMAGE_TAG}" "$(winpath "${K8S_DIR}/..")"

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

echo "images loaded into cluster ${CLUSTER_NAME}: ${IMAGE_TAG}, ${DOWNSTREAM_IMAGE}"
