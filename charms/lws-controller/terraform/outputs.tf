output "app_name" {
  value = juju_application.lws_controller.name
}

output "provides" {
  value = {
    lws_controller = "lws-controller"
  }
}

output "requires" {
  value = {
    logging = "logging"
  }
}
