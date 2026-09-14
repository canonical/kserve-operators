output "app_name" {
  value = juju_application.keda.name
}

output "provides" {
  value = {
    keda = "keda"
  }
}
