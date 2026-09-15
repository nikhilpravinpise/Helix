provider "kind" {}

provider "docker" {
  # npipe on Windows/Docker Desktop; override to unix:///var/run/docker.sock
  # on Linux via terraform.tfvars.
  host = var.docker_host
}

# Reads the kubeconfig kind writes at the default path - the
# kind-<cluster> context exists there whether Terraform or `kind create`
# made the cluster. Consequence, universal to k8s providers rather than
# specific to this setup: the provider needs a readable kubeconfig at plan
# time, so on a clean machine bring-up is two commands -
# `terraform apply -target=kind_cluster.sandbox` then `terraform apply` -
# see README.md / docs/bring-up.md.
provider "kubectl" {
  config_path    = pathexpand("~/.kube/config")
  config_context = "kind-${local.cluster_name}"
}
