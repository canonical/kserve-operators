# Terraform module for llm-integrator

This is a Terraform module facilitating the deployment of the llm-integrator charm, using the [Terraform juju provider](https://github.com/juju/terraform-provider-juju/). For more information, refer to the provider [documentation](https://registry.terraform.io/providers/juju/juju/latest/docs)

## Requirements
This module requires a `juju` model to be available. Refer to the [usage section](#usage) below for more details.

## API

### Inputs
The module offers the following configurable inputs:

| Name | Type | Description | Required |
| - | - | - | - |
| `app_name`| string | Application name | False |
| `base`| string | Charm base | False |
| `channel`| string | Channel that the charm is deployed from | False |
| `config`| map(string) | Map of the charm configuration options | False |
| `model_name`| string | Name of the model that the charm is deployed on | True |
| `resources`| map(string) | Map of the charm resources | False |
| `revision`| number | Revision number of the charm name | False |

### Outputs
Upon applied, the module exports the following outputs:

| Name | Description |
| - | - |
| `app_name`| Application name |
| `provides`| Map of `provides` endpoints |
| `requires`| Map of `requires` endpoints |
