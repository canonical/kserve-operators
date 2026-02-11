# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path
from typing import Optional

import lightkube
import lightkube.codecs
import lightkube.generic_resource
import tenacity
import yaml
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from charmed_kubeflow_chisme.testing import assert_path_reachable_through_ingress
from jinja2 import Template
from lightkube.core.exceptions import ApiError
from lightkube.resources.core_v1 import Pod, Secret, ServiceAccount

from tests.integration.constants import APP_NAME, ISVC

KSERVE_WORKLOAD_CONTAINER = "kserve-container"

logger = logging.getLogger(__name__)


def populate_template(template_path, context):
    """Populates a YAML template with values from the provided context.

    Args:
        template_path (str): Path to the YAML file that serves as the Jinja2 template.
        context (dict): Dictionary of values to render into the template.

    Returns:
        dict: The rendered YAML content as a Python dictionary.
    """
    with open(template_path, "r") as f:
        template = f.read()

    populated_template = Template(template).render(context)
    populated_template_yaml = yaml.safe_load(populated_template)

    return populated_template_yaml


def deploy_k8s_resources(template_files: str):
    """Deploy k8s resources from template files."""
    lightkube_client = lightkube.Client(field_manager=APP_NAME)
    k8s_resource_handler = KubernetesResourceHandler(
        field_manager=APP_NAME, template_files=template_files, context={}
    )
    lightkube.generic_resource.load_in_cluster_generic_resources(lightkube_client)
    k8s_resource_handler.apply()


def delete_all_from_yaml(yaml_text: str, lightkube_client: Optional[lightkube.Client] = None):
    """Deletes all k8s resources listed in a YAML file via lightkube.

    Args:
        yaml_file (str or Path): Either a string filename or a string of valid YAML.  Will attempt
                                 to open a filename at this path, failing back to interpreting the
                                 string directly as YAML.
        lightkube_client: Instantiated lightkube client or None
    """

    if lightkube_client is None:
        lightkube_client = lightkube.Client()

    for obj in lightkube.codecs.load_all_yaml(yaml_text):
        lightkube_client.delete(type(obj), obj.metadata.name)


def safe_load_file_to_text(filename: str) -> str:
    """Returns the contents of filename if it is an existing file, else it returns filename."""
    try:
        text = Path(filename).read_text()
    except FileNotFoundError:
        text = filename
    return text


def print_inf_svc_logs(lightkube_client: lightkube.Client, inf_svc, tail_lines: int = 50):
    """Prints the logs for kserve serving container in the Pod backing an InferenceService.

    Args:
        lightkube_client: Client to connect to kubernetes
        inf_svc: An InferenceService generic resource
        tail_lines: Integer number of lines to print when printing pod logs for debugging
    """
    logger.info(
        f"Printing logs for InferenceService {inf_svc.metadata.name} in namespace {inf_svc.metadata.namespace}"
    )
    pods = list(
        lightkube_client.list(
            Pod,
            labels={"serving.kserve.io/inferenceservice": inf_svc.metadata.name},
            namespace=inf_svc.metadata.namespace,
        )
    )
    if len(pods) > 0:
        printed_logs = False
        pod = pods[0]
        try:
            for line in lightkube_client.log(
                name=pod.metadata.name,
                namespace=pod.metadata.namespace,
                container=KSERVE_WORKLOAD_CONTAINER,
                tail_lines=tail_lines,
            ):
                printed_logs = True
                logger.info(line.strip())
            if not printed_logs:
                logger.info("No logs found - the pod might still be starting up")
        except ApiError:
            logger.info("Failed to retrieve logs - the pod might still be starting up")
    else:
        logger.info("No Pods found - the pod might not be launched yet")


# Helpers for fetching resource dispatcher resources
@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=15),
    stop=tenacity.stop_after_delay(60 * 5),
    reraise=True,
)
def get_k8s_secret(name: str, namespace: str, lightkube_client: lightkube.Client) -> Secret:
    """Returns a k8s secret with retry logic."""
    return lightkube_client.get(Secret, name, namespace=namespace)


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=15),
    stop=tenacity.stop_after_delay(60 * 5),
    reraise=True,
)
def get_k8s_service_account(
    name: str, namespace: str, lightkube_client: lightkube.Client
) -> ServiceAccount:
    """Returns a k8s service account with retry logic."""
    return lightkube_client.get(ServiceAccount, name, namespace=namespace)


# Assert helpers
async def assert_isvc_ingress_traffic(
    isvc_name: str,
    namespace: str,
    lightkube_client: lightkube.Client,
    gateway_namespace="kubeflow",
):
    isvc = lightkube_client.get(ISVC, isvc_name, namespace=namespace)
    isvc_url = isvc.get("status", {}).get("url", "").replace("http://", "")
    assert isvc_url

    headers = {"Host": isvc_url}
    logger.info("Querying %s with headers: %s", isvc_url, headers)

    # Tensorflow returns an error for that endpoint:
    # "error": "Missing model name in request."
    expected_status = 200
    if "tensorflow" in isvc_name:
        expected_status = 400

    await assert_path_reachable_through_ingress(
        http_path="/v1/models",
        namespace=gateway_namespace,
        headers=headers,
        expected_status=expected_status,
        expected_response_text="model",
    )


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=30),
    stop=tenacity.stop_after_attempt(30),
    reraise=True,
)
def assert_inf_svc_state(lightkube_client: lightkube.Client, inf_svc_name, namespace):
    """Checks if a InferenceService is in a ready state by retrying."""
    inf_svc = lightkube_client.get(ISVC, inf_svc_name, namespace=namespace)
    conditions = inf_svc.get("status", {}).get("conditions")
    logger.info(
        f"INFO: Inspecting InferenceService {inf_svc.metadata.name} in namespace {inf_svc.metadata.namespace}"
    )

    status_overall = False
    for condition in conditions:
        if condition.get("status") in ["False", "Unknown"] and condition.get("type") != "Stopped":
            logger.info(f"Inference service is not ready according to condition: {condition}")
            status_overall = False
            print_inf_svc_logs(lightkube_client=lightkube_client, inf_svc=inf_svc)
            break

        status_overall = True
        logger.info("Service is ready")

    assert status_overall is True
