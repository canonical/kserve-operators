output "app_name" {
  value = juju_application.kserve_llmisvc.name
}

output "provides" {
  value = {
    metrics_endpoint = "metrics-endpoint"
  }
}

output "requires" {
  value = {
    kserve_controller = "kserve-controller"
    logging           = "logging"
  }
}
