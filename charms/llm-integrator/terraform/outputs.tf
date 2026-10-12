output "app_name" {
  value = juju_application.llm_integrator.name
}

output "provides" {
  value = {}
}

output "requires" {
  value = {
    kserve_llmisvc = "kserve-llmisvc"
    s3_credentials = "s3-credentials"
    keda           = "keda"
    prometheus_api = "prometheus-api"
  }
}
