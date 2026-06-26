#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the kserve-controller `s3-credentials` relation.

These tests deploy kserve-controller (in standard/ambient mode) together with
an s3-integrator charm and assert that:
  1. Deploying an inferenceservice works
  2. The S3 credentials provided over the `s3-credentials` relation are
    rendered into the Secret and ServiceAccount that are dispatched,
    via the resource-dispatcher, into user namespaces.
"""

import base64
import json
import logging
from pathlib import Path

import lightkube
import lightkube.codecs
import pytest
import tenacity
import yaml
from charmed_kubeflow_chisme.testing import (
    assert_alert_rules,
    assert_logging,
    assert_metrics_endpoint,
    assert_security_context,
    deploy_and_assert_grafana_agent,
    get_alert_rules,
    get_pod_names,
)
from charmed_kubeflow_chisme.testing.s3_integration import (
    deploy_and_assert_s3_integrator,
    host_ip,
)
from lightkube import Client
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.resources.core_v1 import ConfigMap, Pod
from pytest_operator.plugin import OpsTest

from tests.integration.charms_dependencies import (
    METACONTROLLER_OPERATOR,
    RESOURCE_DISPATCHER,
    S3_INTEGRATOR,
)
from tests.integration.constants import (
    APP_NAME,
    CONFIGMAP_DATA_INGRESS_DOMAIN,
    CONFIGMAP_NAME,
    CONFIGMAP_TEMPLATE_PATH,
    CONTAINERS_SECURITY_CONTEXT_MAP,
    CUSTOM_IMAGES_PATH,
    MANIFESTS_SUFFIX,
    METADATA,
    PODDEFAULTS_CRD_TEMPLATE,
    SKLEARN_INF_SVC_NAME,
    SKLEARN_INF_SVC_OBJECT,
    YAMLS_PREFIX,
)
from tests.integration.utils import (
    assert_inf_svc_state,
    assert_isvc_ingress_traffic,
    deploy_k8s_resources,
    get_k8s_secret,
    get_k8s_service_account,
    populate_template,
)

logger = logging.getLogger(__name__)

# tenacity
RETRY_FOR_THREE_MINUTES = tenacity.Retrying(
    stop=tenacity.stop_after_delay(60 * 3),
    wait=tenacity.wait_fixed(5),
    reraise=True,
)

# ambient-mode Istio:
ISTIO_K8S_APP = "istio-k8s"
ISTIO_INGRESS_K8S_APP = "istio-ingress-k8s"
ISTIO_BEACON_K8S_APP = "istio-beacon-k8s"
ISTIO_INGRESS_GATEWAY_ENDPOINT = "gateway-metadata"
SERVICE_MESH_ENDPOINT = "service-mesh"


custom_images = json.loads(Path(CUSTOM_IMAGES_PATH).read_text())

explainer_image, explainer_version = custom_images["configmap__explainers__art"].split(":")


def generate_configmap_context(ingress_gateway_namespace: str) -> dict:
    return {
        **custom_images,
        "configmap__explainers__art__image": explainer_image,
        "configmap__explainers__art__version": explainer_version,
        "deployment_mode": "Standard",
        "enable_gateway_api": "true",
        "ingress_domain": CONFIGMAP_DATA_INGRESS_DOMAIN,
        "ingress_gateway_namespace": ingress_gateway_namespace,
        "ingress_gateway_name": ISTIO_INGRESS_K8S_APP,
    }


@pytest.mark.skip_if_deployed
@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest, request):
    """Build and deploy kserve-controller with the ambient Istio dependencies.

    Assert that the charm reaches Active before any storage relation is added.
    """
    # Deploy ambient Istio ecosystem
    istio_channel = "2/stable"
    await ops_test.model.deploy(
        ISTIO_K8S_APP,
        channel=istio_channel,
        config={"platform": ""},
        trust=True,
    )
    await ops_test.model.deploy(
        ISTIO_INGRESS_K8S_APP,
        channel=istio_channel,
        trust=True,
    )
    await ops_test.model.deploy(
        ISTIO_BEACON_K8S_APP,
        channel=istio_channel,
        trust=True,
        config={"model-on-mesh": False},
    )
    await ops_test.model.wait_for_idle(
        raise_on_blocked=False,
        raise_on_error=False,
        wait_for_active=True,
        timeout=900,
    )

    # build and deploy charm from local source folder
    entity_url = (
        await ops_test.build_charm(".")
        if not (entity_url := request.config.getoption("--charm-path"))
        else entity_url
    )
    resources = {
        "kserve-controller-image": METADATA["resources"]["kserve-controller-image"][
            "upstream-source"
        ]
    }
    await ops_test.model.deploy(
        entity_url,
        resources=resources,
        config={"deployment-mode": "standard"},
        application_name=APP_NAME,
        trust=True,
    )

    await ops_test.model.integrate(
        f"{ISTIO_INGRESS_K8S_APP}:{ISTIO_INGRESS_GATEWAY_ENDPOINT}",
        f"{APP_NAME}:{ISTIO_INGRESS_GATEWAY_ENDPOINT}",
    )
    await ops_test.model.integrate(
        f"{ISTIO_BEACON_K8S_APP}:{SERVICE_MESH_ENDPOINT}",
        f"{APP_NAME}:{SERVICE_MESH_ENDPOINT}",
    )

    # issuing dummy update_status just to trigger an event
    async with ops_test.fast_forward(fast_interval="60s"):
        await ops_test.model.wait_for_idle(
            apps=[APP_NAME],
            status="active",
            raise_on_blocked=False,
            timeout=1000,
        )
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"

    # Deploying grafana-agent-k8s and add all relations
    await deploy_and_assert_grafana_agent(
        ops_test.model, APP_NAME, metrics=True, dashboard=False, logging=True
    )


async def test_relate_to_s3_integrator(ops_test: OpsTest):
    """Test that the charm can relate to s3-integrator and stay in Active state."""
    # Deploy s3-integrator and provide it with S3 credentials
    await deploy_and_assert_s3_integrator(ops_test.model, s3_integrator=S3_INTEGRATOR)

    await ops_test.model.integrate(
        f"{APP_NAME}:s3-credentials", f"{S3_INTEGRATOR.charm}:s3-credentials"
    )
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME, S3_INTEGRATOR.charm],
        status="active",
        raise_on_blocked=False,
        raise_on_error=False,
        timeout=600,
    )
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"


@pytest.mark.parametrize(
    "inference_file",
    [
        YAMLS_PREFIX + "predictionserver-sklearn.yaml",
        YAMLS_PREFIX + "sklearn-iris.yaml",
        YAMLS_PREFIX + "lgbserver.yaml",
        YAMLS_PREFIX + "pmml-server.yaml",
        YAMLS_PREFIX + "paddleserver-resnet.yaml",
        YAMLS_PREFIX + "xgbserver.yaml",
        YAMLS_PREFIX + "tensorflow-serving.yaml",
    ],
)
async def test_inference_service(
    test_namespace: str,
    lightkube_client: lightkube.Client,
    inference_file,
    ops_test: OpsTest,
):
    """Validates that an InferenceService can be deployed."""
    inf_svc_yaml = yaml.safe_load(Path(inference_file).read_text())
    inf_svc_object = lightkube.codecs.load_all_yaml(yaml.dump(inf_svc_yaml))[0]
    inf_svc_name = inf_svc_object.metadata.name

    # Create InferenceService from example file
    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=15),
        stop=tenacity.stop_after_delay(30),
        reraise=True,
    )
    def create_inf_svc():
        lightkube_client.create(inf_svc_object, namespace=test_namespace)

    create_inf_svc()
    # Assert InferenceService state is Available
    assert_inf_svc_state(lightkube_client, inf_svc_name, test_namespace)

    # Assert that traffic reaches the ISVC
    await assert_isvc_ingress_traffic(
        inf_svc_name, test_namespace, lightkube_client, ops_test.model_name
    )


# Test o11y
async def test_logging(ops_test: OpsTest):
    """Test logging is defined in relation data bag."""
    app = ops_test.model.applications[APP_NAME]
    await assert_logging(app)


async def test_metrics_endpoint(ops_test: OpsTest):
    """Test metrics_endpoints are defined in relation data bag and their accessibility.
    This function gets all the metrics_endpoints from the relation data bag, checks if
    they are available from the grafana-agent-k8s charm and finally compares them with the
    ones provided to the function.
    """
    app = ops_test.model.applications[APP_NAME]
    await assert_metrics_endpoint(app, metrics_port=8080, metrics_path="/metrics")


async def test_alert_rules(ops_test: OpsTest):
    """Test check charm alert rules and rules defined in relation data bag."""
    app = ops_test.model.applications[APP_NAME]
    alert_rules = get_alert_rules()
    logger.info("found alert_rules: %s", alert_rules)
    await assert_alert_rules(app, alert_rules)


# Test KServe ConfigMap
async def test_configmap_created(lightkube_client: lightkube.Client, ops_test: OpsTest):
    """
    Test whether the configmap is created with the expected data.

    Args:
        lightkube_client (lightkube.Client): The Lightkube client to interact with Kubernetes.
        ops_test (OpsTest): The Juju OpsTest fixture to interact with the deployed model.
    """
    inferenceservice_config = lightkube_client.get(
        ConfigMap, CONFIGMAP_NAME, namespace=ops_test.model_name
    )

    expected_configmap = populate_template(
        CONFIGMAP_TEMPLATE_PATH, generate_configmap_context(ops_test.model_name)
    )
    assert inferenceservice_config.data == expected_configmap["data"]


async def test_configmap_changes_with_config(
    lightkube_client: lightkube.Client, ops_test: OpsTest
):
    """
    Test whether the configmap changes successfully with custom configurations.

    Args:
        lightkube_client (lightkube.Client): The Lightkube client to interact with Kubernetes.
        ops_test (OpsTest): The Juju OpsTest fixture to interact with the deployed model.
    """
    await ops_test.model.applications[APP_NAME].set_config(
        {"custom_images": '{"configmap__batcher": "custom:1.0"}'}  # noqa: E501
    )
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=300
    )

    inferenceservice_config = lightkube_client.get(
        ConfigMap, CONFIGMAP_NAME, namespace=ops_test.model_name
    )

    configmap_context = generate_configmap_context(ops_test.model_name)
    configmap_context["configmap__batcher"] = "custom:1.0"

    expected_configmap = populate_template(CONFIGMAP_TEMPLATE_PATH, configmap_context)
    assert inferenceservice_config.data == expected_configmap["data"]


async def test_deploy_resource_dispatcher(ops_test: OpsTest):
    """Test that the charm can relate to resource-dispatcher and stay in Active state.

    We need to deploy Metacontroller and poddefaults CRD (for Resource dispatcher).
    """
    deploy_k8s_resources([PODDEFAULTS_CRD_TEMPLATE])
    await ops_test.model.deploy(
        entity_url=METACONTROLLER_OPERATOR.charm,
        channel=METACONTROLLER_OPERATOR.channel,
        trust=METACONTROLLER_OPERATOR.trust,
    )
    await ops_test.model.wait_for_idle(
        apps=[METACONTROLLER_OPERATOR.charm],
        status="active",
        raise_on_blocked=False,
        raise_on_error=False,
        timeout=120,
    )
    await ops_test.model.deploy(
        RESOURCE_DISPATCHER.charm,
        channel=RESOURCE_DISPATCHER.channel,
        trust=RESOURCE_DISPATCHER.trust,
    )
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME],
        status="active",
        raise_on_blocked=False,
        raise_on_error=False,
        timeout=120,
        idle_period=60,
    )

    await ops_test.model.integrate(
        f"{APP_NAME}:service-accounts", f"{RESOURCE_DISPATCHER.charm}:service-accounts"
    )
    await ops_test.model.integrate(f"{APP_NAME}:secrets", f"{RESOURCE_DISPATCHER.charm}:secrets")

    await ops_test.model.wait_for_idle(
        apps=[RESOURCE_DISPATCHER.charm, APP_NAME],
        status="active",
        raise_on_blocked=False,
        raise_on_error=False,
        timeout=1200,
    )
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"


async def test_new_user_namespace_has_s3_manifests(
    ops_test: OpsTest, lightkube_client: lightkube.Client, test_namespace: str
):
    """Create a user namespace and check the dispatched S3 Secret and ServiceAccount.

    The Secret is built from the data provided over the `s3-credentials` relation
    by s3-integrator.
    """
    logger.info("Checking the created secret in the user namespace.")
    manifests_name = f"{APP_NAME}{MANIFESTS_SUFFIX}"
    secret = get_k8s_secret(manifests_name, test_namespace, lightkube_client)
    service_account = get_k8s_service_account(manifests_name, test_namespace, lightkube_client)

    annotations = secret.metadata.annotations
    assert annotations["serving.kserve.io/s3-endpoint"] == host_ip()
    assert annotations["serving.kserve.io/s3-usehttps"]
    assert annotations["serving.kserve.io/s3-useanoncredential"] == "false"
    assert annotations["serving.kserve.io/s3-region"]

    # The credentials are generated by microceph and are not known ahead of time,
    # so assert that they are present and non-empty rather than matching values.
    assert base64.b64decode(secret.data["AWS_ACCESS_KEY_ID"])
    assert base64.b64decode(secret.data["AWS_SECRET_ACCESS_KEY"])

    assert service_account.secrets[0].name == manifests_name


# Test Proxy configurations
async def test_inference_service_proxy_envs_configuration(
    test_namespace: str, ops_test: OpsTest, lightkube_client: lightkube.Client
):
    """Changes `http-proxy`, `https-proxy` and `no-proxy` configs and asserts that
    the InferenceService Pod is using the values from configs as environment variables."""

    # Set Proxy envs by setting the charm configs
    test_http_proxy = "my_http_proxy"
    test_https_proxy = "my_https_proxy"
    test_no_proxy = "no_proxy"

    await ops_test.model.applications[APP_NAME].set_config(
        {
            "http-proxy": test_http_proxy,
            "https-proxy": test_https_proxy,
            "no-proxy": test_no_proxy,
        }
    )

    await ops_test.model.wait_for_idle(
        [APP_NAME],
        status="active",
        raise_on_blocked=False,
        timeout=60 * 1,
    )

    # Create InferenceService from example file
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            lightkube_client.create(SKLEARN_INF_SVC_OBJECT, namespace=test_namespace)

    # Assert InferenceService Pod specifies the proxy envs for the initContainer
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            pods_list = iter(
                lightkube_client.list(
                    res=Pod,
                    namespace=test_namespace,
                    labels={"serving.kserve.io/inferenceservice": SKLEARN_INF_SVC_NAME},
                )
            )
            isvc_pod = next(pods_list)
            init_env_vars = isvc_pod.spec.initContainers[0].env

            http_proxy_env = https_proxy_env = no_proxy_env = None
            for env_var in init_env_vars:
                if env_var.name == "HTTP_PROXY":
                    http_proxy_env = env_var.value
                elif env_var.name == "HTTPS_PROXY":
                    https_proxy_env = env_var.value
                elif env_var.name == "NO_PROXY":
                    no_proxy_env = env_var.value

            assert http_proxy_env == test_http_proxy
            assert https_proxy_env == test_https_proxy
            assert no_proxy_env == test_no_proxy


async def test_blocked_on_invalid_config(ops_test: OpsTest):
    """
    Test whether the application is blocked on providing an invalid configuration.

    Args:
        ops_test (OpsTest): The Juju OpsTest fixture to interact with the deployed model.
    """
    await ops_test.model.applications[APP_NAME].set_config({"custom_images": "{"})
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME], status="blocked", raise_on_blocked=False, timeout=300
    )
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "blocked"


@pytest.mark.parametrize("container_name", list(CONTAINERS_SECURITY_CONTEXT_MAP.keys()))
async def test_container_security_context(
    ops_test: OpsTest,
    lightkube_client: Client,
    container_name: str,
):
    """Test container security context is correctly set.

    Verify that container spec defines the security context with correct
    user ID and group ID.
    """
    pod_name = get_pod_names(ops_test.model.name, APP_NAME)[0]
    assert_security_context(
        lightkube_client,
        pod_name,
        container_name,
        CONTAINERS_SECURITY_CONTEXT_MAP,
        ops_test.model.name,
    )


@pytest.mark.abort_on_fail
async def test_remove_with_resources_present(ops_test: OpsTest):
    """Test remove with all resources deployed.

    Verify that all deployed resources that need to be removed are removed.

    This test should be next after test_upgrade(), because it removes deployed charm.
    """

    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=15),
        stop=tenacity.stop_after_delay(5 * 60),
        reraise=True,
    )
    def assert_resources_removed():
        """Asserts on the resource removal.

        Retries multiple times using tenacity to allow time for the resources to be deleted.
        """
        lightkube_client = lightkube.Client()
        crd_list = iter(
            lightkube_client.list(
                CustomResourceDefinition,
                labels=[("app.juju.is/created-by", APP_NAME)],
                namespace=ops_test.model_name,
            )
        )
        # testing for empty list (iterator)
        _last = object()
        assert next(crd_list, _last) is _last

    # remove deployed charm and verify that it is removed alongside resources it created
    await ops_test.model.remove_application(app_name=APP_NAME, block_until_done=True)
    assert APP_NAME not in ops_test.model.applications

    assert_resources_removed()
