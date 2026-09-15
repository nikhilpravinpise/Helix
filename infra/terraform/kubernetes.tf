# In-cluster objects, applied from the repo's own YAML files via the
# kubectl provider - the files stay the single source of truth for both
# the script path and the Terraform path.
#
# Ordering that matters:
#   1. kind cluster exists          (kubectl provider credentials)
#   2. sandbox images loaded        (payment-api/deployment would sit
#                                    ImagePullBackOff otherwise)
#   3. ingress-nginx controller up  (the ValidatingWebhookConfiguration
#                                    would REJECT Ingress creation while
#                                    its webhook is not serving yet)
#   4. payment-api + helix ingress objects

data "kubectl_path_documents" "ingress_nginx" {
  pattern = "${local.repo_root}/sandbox/payment-api/k8s/ingress-nginx/deploy.yaml"
}

data "kubectl_path_documents" "payment_api" {
  pattern = "${local.repo_root}/sandbox/payment-api/k8s/*.yaml"
}

data "kubectl_path_documents" "helix_ingress" {
  pattern = "${local.repo_root}/infra/helix-ingress.yaml"
}

# NOTE: filebeat-daemonset.yaml cannot go through kubectl_path_documents -
# that data source renders files as HCL templates, and the manifest's own
# `${ES_HOST}`/`%{+yyyy.MM.dd}` placeholders are metacharacters there. It
# is applied via kubectl below instead - the one documented exception to
# "terraform manages k8s objects as kubectl_manifest resources".

resource "kubectl_manifest" "ingress_nginx" {
  for_each  = data.kubectl_path_documents.ingress_nginx.manifests
  yaml_body = each.value
  # Waits the controller Deployment's rollout out, so the admission
  # webhook is serving before any Ingress object below is applied.
  wait_for_rollout = true
  depends_on       = [terraform_data.sandbox_images]
}

resource "kubectl_manifest" "payment_api" {
  for_each         = data.kubectl_path_documents.payment_api.manifests
  yaml_body        = each.value
  wait_for_rollout = true
  depends_on       = [kubectl_manifest.ingress_nginx]
}

# helix.local -> the Helix API running on the HOST (ExternalName ->
# host.docker.internal). Docker Desktop only; on a Linux kind host swap in
# the selectorless-Service + manual-Endpoints variant documented in
# infra/helix-ingress.yaml's header.
resource "kubectl_manifest" "helix_ingress" {
  for_each   = data.kubectl_path_documents.helix_ingress.manifests
  yaml_body  = each.value
  depends_on = [kubectl_manifest.ingress_nginx]
}

# Log shippers into the host ES (see the NOTE above for why this is a
# local-exec apply rather than kubectl_manifest). Only meaningful when the
# logging stack is enabled - filebeat retries forever until ES answers,
# but there is no point running it with no store.
resource "terraform_data" "filebeat" {
  count = var.enable_logging ? 1 : 0

  triggers_replace = [filesha256("${local.repo_root}/infra/filebeat-daemonset.yaml")]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = "kubectl --context kind-${local.cluster_name} apply -f ${local.repo_root}/infra/filebeat-daemonset.yaml"
  }

  depends_on = [kind_cluster.sandbox]
}
