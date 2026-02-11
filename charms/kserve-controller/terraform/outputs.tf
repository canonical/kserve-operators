output "app_name" {
  value = juju_application.kserve_controller.name
}

output "provides" {
  value = {
    metrics_endpoint = "metrics-endpoint",
    provide_cmr_mesh = "provide-cmr-mesh"
  }
}

output "requires" {
  value = {
    gateway_metadata = "gateway-metadata",
    ingress_gateway  = "ingress-gateway",
    local_gateway    = "local-gateway",
    logging          = "logging",
    object_storage   = "object-storage",
    require_cmr_mesh = "require-cmr-mesh",
    secrets          = "secrets",
    service_accounts = "service-accounts",
    service_mesh     = "service-mesh"
  }
}
