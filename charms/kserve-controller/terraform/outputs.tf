output "app_name" {
  value = juju_application.kserve_controller.name
}

output "provides" {
  value = {
    metrics_endpoint = "metrics-endpoint"
  }
}

output "requires" {
  value = {
    object_storage   = "object-storage",
    ingress_gateway  = "ingress-gateway",
    local_gateway    = "local-gateway",
    secrets          = "secrets",
    service_accounts = "service-accounts",
    logging          = "logging",
  }
}
