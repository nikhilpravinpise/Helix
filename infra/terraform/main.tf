# The kind sandbox cluster + the container images the cluster needs.
#
# kind_config is generated FROM infra/kind-config.yaml - the file stays the
# single source of truth, shared with scripts/up.sh, so the script path and
# the Terraform path can never drift on port mappings.
#
# NOTE on adopting an existing cluster: the kind provider has no import
# support (no Importer on the resource - verified in the provider source).
# A cluster created by `kind create` outside Terraform cannot be adopted.
# The one-time transition is: `kind delete cluster --name helix-sandbox`,
# then `terraform apply` recreates it under management (the sandbox is
# stateless - everything in it comes from this config or the manifests).

locals {
  repo_root   = abspath(var.repo_root)
  kind_config = yamldecode(file(var.kind_config_path))
  # The file owns the name - var.cluster_name exists only for callers that
  # need it documented; everything below reads the file so they cannot drift.
  cluster_name = local.kind_config.name
}

resource "kind_cluster" "sandbox" {
  name           = local.cluster_name
  wait_for_ready = true
  # Writes/merges the kind-<name> context into the default kubeconfig,
  # which is what the kubectl provider (and scripts/*.sh) reads.

  kind_config {
    kind        = local.kind_config.kind
    api_version = local.kind_config.apiVersion

    dynamic "node" {
      for_each = local.kind_config.nodes
      content {
        role = node.value.role

        dynamic "extra_port_mappings" {
          for_each = try(node.value.extraPortMappings, [])
          content {
            container_port = extra_port_mappings.value.containerPort
            host_port      = extra_port_mappings.value.hostPort
            listen_address = try(extra_port_mappings.value.listenAddress, null)
            protocol       = try(extra_port_mappings.value.protocol, null)
          }
        }
      }
    }
  }
}

# Build + load the sandbox images into the cluster, exactly the way
# scripts/up.sh does it (the shared implementation lives in the script so
# both bring-up paths behave identically, including the multi-arch
# docker-save workaround documented there).
#
# interpreter: Git Bash. Every scripts/*.sh is bash - a clean machine needs
# Git for Windows on PATH ahead of any WSL bash (see README).
locals {
  payment_api_files = concat(
    ["Dockerfile", "pyproject.toml", "uv.lock"],
    tolist(fileset("${local.repo_root}/sandbox/payment-api", "payment_api/**")),
  )
  payment_api_hash = sha256(join("", [
    for f in local.payment_api_files :
    filesha256("${local.repo_root}/sandbox/payment-api/${f}")
  ]))
}

resource "terraform_data" "sandbox_images" {
  # Rebuild/reload only when payment-api source actually changes.
  triggers_replace = [local.payment_api_hash]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = "${local.repo_root}/scripts/load-sandbox-images.sh"
  }

  depends_on = [kind_cluster.sandbox]
}
