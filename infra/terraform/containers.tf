# Host-side containers - the observer plane (D1/D3): everything that
# watches the cluster lives on the host, not inside the cluster being
# deliberately broken. These mirror the compose stacks under infra/ (same
# images, env, mem limits, ports, healthchecks) - Terraform is the
# documented bring-up path, the compose files + scripts/*.sh remain the
# escape hatch. The two paths are alternatives for the SAME service set;
# they bind the same host ports and are not meant to run mixed at once.
#
# Volume names deliberately match what compose would create
# (project_volume), so whichever path created the volume first is the one
# the other adopts - Postgres state survives a switch between the two.

resource "docker_network" "helix" {
  name = "helix"
}

# The "kind" network is created by `kind create cluster`; Jenkins attaches
# to it to reach the control-plane container by name (see
# infra/jenkins-compose.yml for why). Read AFTER the cluster exists so the
# lookup defers to apply time on a clean machine.
data "docker_network" "kind" {
  name = "kind"

  depends_on = [kind_cluster.sandbox]
}

# --- Postgres: Helix incident state (D3) --------------------------------

resource "docker_volume" "postgres_data" {
  count = var.enable_postgres ? 1 : 0
  # Same name compose would give it: helix-state_postgres-data.
  name = "helix-state_postgres-data"

  # A volume already created by the compose path carries compose labels;
  # diffing them would force replacement - i.e. DELETE the data. The
  # resource claims the name, the volume's own metadata stays as-is.
  lifecycle {
    ignore_changes = [labels]
  }
}


resource "docker_image" "postgres" {
  count        = var.enable_postgres ? 1 : 0
  name         = "postgres:16-alpine"
  keep_locally = true
}

resource "docker_container" "postgres" {
  count   = var.enable_postgres ? 1 : 0
  name    = "helix-postgres"
  image   = docker_image.postgres[0].image_id
  restart = "unless-stopped"
  memory  = 512
  # Docker defaults memory-swap to 2x memory; set it explicitly or the
  # provider diffs it forever.
  memory_swap = 1024

  env = [
    "POSTGRES_USER=${var.postgres_user}",
    "POSTGRES_PASSWORD=${var.postgres_password}",
    "POSTGRES_DB=${var.postgres_db}",
  ]

  ports {
    internal = 5432
    external = 5432
    ip       = "127.0.0.1"
  }

  volumes {
    volume_name    = docker_volume.postgres_data[0].name
    container_path = "/var/lib/postgresql/data"
  }

  healthcheck {
    test     = ["CMD-SHELL", "pg_isready -U ${var.postgres_user} -d ${var.postgres_db}"]
    interval = "5s"
    timeout  = "5s"
    retries  = 20
  }

  networks_advanced {
    name    = docker_network.helix.name
    aliases = ["postgres"]
  }
}

# --- Elasticsearch + Kibana: the log store -------------------------------

resource "docker_image" "elasticsearch" {
  count        = var.enable_logging ? 1 : 0
  name         = "docker.elastic.co/elasticsearch/elasticsearch:8.15.3"
  keep_locally = true
}

resource "docker_container" "elasticsearch" {
  count       = var.enable_logging ? 1 : 0
  name        = "helix-elasticsearch"
  image       = docker_image.elasticsearch[0].image_id
  memory      = 1024
  memory_swap = 2048

  env = [
    "discovery.type=single-node",
    "xpack.security.enabled=false",
    "xpack.security.http.ssl.enabled=false",
    "ES_JAVA_OPTS=-Xms512m -Xmx512m",
  ]

  # All interfaces, like compose's "9200:9200": pods inside the kind
  # cluster reach this over the docker bridge gateway (filebeat).
  ports {
    internal = 9200
    external = 9200
  }

  healthcheck {
    test     = ["CMD-SHELL", "curl -sf http://localhost:9200/_cluster/health || exit 1"]
    interval = "5s"
    timeout  = "5s"
    retries  = 40
  }

  networks_advanced {
    name    = docker_network.helix.name
    aliases = ["elasticsearch"]
  }
}

resource "docker_image" "kibana" {
  count        = var.enable_logging ? 1 : 0
  name         = "docker.elastic.co/kibana/kibana:8.15.3"
  keep_locally = true
}

resource "docker_container" "kibana" {
  count       = var.enable_logging ? 1 : 0
  name        = "helix-kibana"
  image       = docker_image.kibana[0].image_id
  memory      = 768
  memory_swap = 1536

  env = [
    "ELASTICSEARCH_HOSTS=http://elasticsearch:9200",
    "XPACK_SECURITY_ENABLED=false",
  ]

  ports {
    internal = 5601
    external = 5601
    ip       = "127.0.0.1"
  }

  networks_advanced {
    name = docker_network.helix.name
  }

  # Creation order only - Kibana retries Elasticsearch itself.
  depends_on = [docker_container.elasticsearch]
}

# --- Jenkins (D1) --------------------------------------------------------
#
# The image is built from infra/jenkins (same build context as
# jenkins-compose.yml). Rebuilds trigger on any change under infra/jenkins.
# The container joins the terraform-managed helix network AND kind's own
# network so the pipeline's internal kubeconfig resolves - see
# infra/jenkins-compose.yml for the full explanation.

locals {
  jenkins_files_hash = sha256(join("", [
    for f in fileset("${local.repo_root}/infra/jenkins", "**") :
    filesha256("${local.repo_root}/infra/jenkins/${f}")
  ]))
}

resource "docker_image" "jenkins" {
  count = var.enable_jenkins ? 1 : 0
  name  = "helix-jenkins:local"
  build {
    context    = "${local.repo_root}/infra/jenkins"
    dockerfile = "Dockerfile"
  }
  triggers = {
    context = local.jenkins_files_hash
  }
}

resource "docker_volume" "jenkins_home" {
  count = var.enable_jenkins ? 1 : 0
  # Same name compose would give it: helix-jenkins_jenkins-home.
  name = "helix-jenkins_jenkins-home"

  # A volume already created by the compose path carries compose labels;
  # diffing them would force replacement - i.e. DELETE the data. The
  # resource claims the name, the volume's own metadata stays as-is.
  lifecycle {
    ignore_changes = [labels]
  }
}


resource "docker_container" "jenkins" {
  count       = var.enable_jenkins ? 1 : 0
  name        = "helix-jenkins"
  image       = docker_image.jenkins[0].image_id
  memory      = 1024
  memory_swap = 2048
  group_add   = [var.jenkins_docker_gid]

  env = [
    "JAVA_OPTS=-Djenkins.install.runSetupWizard=false -Xmx512m",
    "JENKINS_ADMIN_ID=admin",
    "JENKINS_ADMIN_PASSWORD=admin-sandbox-placeholder",
    # Placeholder unless a real token is supplied - see infra/jenkins/
    # jenkins.yaml and docs/bring-up.md for the SonarQube token step.
    "SONARQUBE_TOKEN=${var.sonarqube_token}",
  ]

  # All interfaces: SonarQube's quality-gate webhook POSTs back via
  # host.docker.internal:8080 - loopback-published ports are not reachable
  # from containers on Docker Desktop (same exception as ES 9200).
  ports {
    internal = 8080
    external = 8080
  }

  volumes {
    volume_name    = docker_volume.jenkins_home[0].name
    container_path = "/var/jenkins_home"
  }

  volumes {
    host_path      = "/var/run/docker.sock"
    container_path = "/var/run/docker.sock"
  }

  volumes {
    host_path      = local.repo_root
    container_path = "/workspace/helix-src"
    read_only      = true
  }

  networks_advanced {
    name    = docker_network.helix.name
    aliases = ["jenkins"]
  }

  networks_advanced {
    name = data.docker_network.kind.name
  }
}

# --- SonarQube: the quality gate -----------------------------------------

resource "docker_volume" "sonarqube_data" {
  count = var.enable_sonarqube ? 1 : 0
  name  = "helix-sonarqube_data"

  # A volume already created by the compose path carries compose labels;
  # diffing them would force replacement - i.e. DELETE the data. The
  # resource claims the name, the volume's own metadata stays as-is.
  lifecycle {
    ignore_changes = [labels]
  }
}


resource "docker_volume" "sonarqube_extensions" {
  count = var.enable_sonarqube ? 1 : 0
  name  = "helix-sonarqube_extensions"

  # A volume already created by the compose path carries compose labels;
  # diffing them would force replacement - i.e. DELETE the data. The
  # resource claims the name, the volume's own metadata stays as-is.
  lifecycle {
    ignore_changes = [labels]
  }
}


resource "docker_volume" "sonarqube_logs" {
  count = var.enable_sonarqube ? 1 : 0
  name  = "helix-sonarqube_logs"

  # A volume already created by the compose path carries compose labels;
  # diffing them would force replacement - i.e. DELETE the data. The
  # resource claims the name, the volume's own metadata stays as-is.
  lifecycle {
    ignore_changes = [labels]
  }
}


resource "docker_image" "sonarqube" {
  count        = var.enable_sonarqube ? 1 : 0
  name         = "sonarqube:26.9.0.129388-community"
  keep_locally = true
}

resource "docker_container" "sonarqube" {
  count       = var.enable_sonarqube ? 1 : 0
  name        = "helix-sonarqube"
  image       = docker_image.sonarqube[0].image_id
  memory      = 3072
  memory_swap = 6144

  env = [
    # Heaps pinned small - this host is memory-tight (CF-4). Elasticsearch
    # inside SonarQube still needs vm.max_map_count>=524288 on the Docker
    # host's Linux VM - see infra/sonarqube-compose.yml for the WSL command.
    "SONAR_WEB_JAVAOPTS=-Xmx512m",
    "SONAR_CE_JAVAOPTS=-Xmx512m",
    "SONAR_SEARCH_JAVAOPTS=-Xms512m -Xmx512m",
  ]

  ulimit {
    name = "nofile"
    soft = 65536
    hard = 65536
  }

  # All interfaces: the Jenkins scanner container reaches this via
  # host.docker.internal:9000 - loopback-published ports are not reachable
  # from containers on Docker Desktop (same exception as ES 9200).
  ports {
    internal = 9000
    external = 9000
  }

  volumes {
    volume_name    = docker_volume.sonarqube_data[0].name
    container_path = "/opt/sonarqube/data"
  }

  volumes {
    volume_name    = docker_volume.sonarqube_extensions[0].name
    container_path = "/opt/sonarqube/extensions"
  }

  volumes {
    volume_name    = docker_volume.sonarqube_logs[0].name
    container_path = "/opt/sonarqube/logs"
  }

  healthcheck {
    test     = ["CMD-SHELL", "curl -sf http://localhost:9000/api/system/status | grep -q '\"status\":\"UP\"' || exit 1"]
    interval = "10s"
    timeout  = "5s"
    # SonarQube boots slowly (embedded ES + DB migrations); give it room.
    retries      = 60
    start_period = "120s"
  }

  networks_advanced {
    name    = docker_network.helix.name
    aliases = ["sonarqube"]
  }
}
