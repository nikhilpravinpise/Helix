# Shared config for the kind sandbox scripts. Sourced, not executed.

CLUSTER_NAME="helix-sandbox"
KUBE_CONTEXT="kind-${CLUSTER_NAME}"
NAMESPACE="payment-api"
K8S_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/sandbox/payment-api/k8s"
IMAGE_TAG="payment-api:kind"
DOWNSTREAM_IMAGE="nginx:1.27-alpine"

kctl() {
  kubectl --context "${KUBE_CONTEXT}" "$@"
}
