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
    lws_controller    = "lws-controller"
    logging           = "logging"
  }
}
