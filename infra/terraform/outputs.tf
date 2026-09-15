output "cluster_name" {
  value = kind_cluster.sandbox.name
}

output "kube_context" {
  value = "kind-${kind_cluster.sandbox.name}"
}

output "cluster_endpoint" {
  value = kind_cluster.sandbox.endpoint
}

output "endpoints" {
  value = {
    payment_api   = "http://payment-api.local (hosts: 127.0.0.1 payment-api.local)"
    helix         = "http://helix.local (hosts: 127.0.0.1 helix.local; needs HELIX_API_BIND=0.0.0.0 helix serve)"
    kibana        = var.enable_logging ? "http://127.0.0.1:5601" : "disabled"
    elasticsearch = var.enable_logging ? "http://127.0.0.1:9200" : "disabled"
    jenkins       = var.enable_jenkins ? "http://127.0.0.1:8080" : "disabled"
    sonarqube     = var.enable_sonarqube ? "http://127.0.0.1:9000" : "disabled"
    postgres      = var.enable_postgres ? "127.0.0.1:5432" : "disabled"
  }
}
