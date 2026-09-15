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

# Normalize a path for native-Windows tools (docker, kind, kubectl). Under
# Git Bash, MSYS auto-translates /c/... paths - but only when
# MSYS_NO_PATHCONV is unset; callers such as terraform local-exec inherit
# whatever environment launched them, so normalize explicitly instead of
# relying on ambient behavior. On Linux, cygpath is absent and the path
# passes through untouched.
winpath() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$1"
  else
    printf '%s' "$1"
  fi
}
