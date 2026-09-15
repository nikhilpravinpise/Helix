terraform {
  required_version = ">= 1.5"

  required_providers {
    kind = {
      source  = "tehcyx/kind"
      version = "~> 0.10"
    }
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 3.0"
    }
    # kubectl provider (alekc fork - the maintained successor of
    # gavinbunney/kubectl) so Terraform applies the repo's real YAML files
    # (kubectl_manifest / kubectl_path_documents) instead of a second copy
    # translated into HCL.
    kubectl = {
      source  = "alekc/kubectl"
      version = "~> 2.1"
    }
  }
}
