variable "kind_config_path" {
  description = "Path to the kind cluster config (port mappings for ingress). Single source of truth shared with scripts/up.sh."
  type        = string
  default     = "../kind-config.yaml"
}

variable "repo_root" {
  description = "Absolute path of the Helix repo checkout."
  type        = string
  default     = "C:/Dev/Helix"
}

variable "docker_host" {
  description = "Docker daemon socket for the docker provider. npipe on Windows/Docker Desktop; unix socket on Linux."
  type        = string
  default     = "npipe:////./pipe/docker_engine"
}

# --- Host-side containers (the observer plane: D1/D3) -------------------
#
# Each service can be switched off for a partial apply on memory-tight
# hosts (CF-4): the sandbox cluster plus Postgres is the minimal useful
# set; add the rest as headroom allows. All default to true so a clean
# machine with enough RAM gets the full environment from one apply.

variable "enable_postgres" {
  description = "Helix incident-state Postgres (D3)."
  type        = bool
  default     = true
}

variable "enable_logging" {
  description = "Elasticsearch + Kibana log store, plus the in-cluster filebeat/events-watcher shippers. ~1.8GB RSS."
  type        = bool
  default     = true
}

variable "enable_jenkins" {
  description = "Jenkins CI container (built from infra/jenkins). Do not run alongside the logging stack on a tight host - see infra/jenkins-compose.yml."
  type        = bool
  default     = true
}

variable "enable_sonarqube" {
  description = "SonarQube quality-gate server. Heaviest single container (~2.5-3GB); needs vm.max_map_count>=524288 on the Docker host's Linux VM (see infra/sonarqube-compose.yml)."
  type        = bool
  default     = true
}

variable "postgres_user" {
  type    = string
  default = "helix"
}

variable "postgres_password" {
  type    = string
  default = "helix"
}

variable "postgres_db" {
  type    = string
  default = "helix"
}

variable "sonarqube_token" {
  description = "Token Jenkins uses to submit analyses; injected into the JCasC sonarqube-token credential. Set via TF_VAR_sonarqube_token or a terraform.tfvars - never commit a real token."
  type        = string
  default     = ""
  sensitive   = true
}

variable "jenkins_docker_gid" {
  description = "GID owning /var/run/docker.sock as seen inside the container, so the non-root jenkins user can use it (`docker exec helix-jenkins stat -c %g /var/run/docker.sock`). Docker Desktop: the socket is root:root 0660 -> 0. Linux hosts: usually the `docker` group's gid."
  type        = string
  default     = "0"
}

variable "image_tag" {
  description = "payment-api image tag built and loaded into kind."
  type        = string
  default     = "payment-api:kind"
}

variable "downstream_image" {
  description = "downstream-stub image pre-loaded into kind (kept deterministic rather than pulled at pod start)."
  type        = string
  default     = "nginx:1.27-alpine"
}
