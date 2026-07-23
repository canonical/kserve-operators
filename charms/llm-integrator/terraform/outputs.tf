output "app_name" {
  value = juju_application.llm_integrator.name
}

output "provides" {
  value = {}
}

output "requires" {
  value = {
    kserve_llmisvc = "kserve-llmisvc"
  }
}
