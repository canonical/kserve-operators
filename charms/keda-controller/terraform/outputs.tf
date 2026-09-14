output "app_name" {
  value = juju_application.keda_controller.name
}

output "provides" {
  value = {
    keda = "keda"
  }
}
