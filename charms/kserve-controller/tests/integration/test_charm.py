#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.


import base64
import logging
import time
from pathlib import Path

import lightkube
import lightkube.codecs
import lightkube.generic_resource
import pytest
import tenacity
import yaml
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from charmed_kubeflow_chisme.testing import (
    assert_alert_rules,
    assert_logging,
    assert_metrics_endpoint,
    deploy_and_assert_grafana_agent,
    get_alert_rules,
)
from lightkube.core.exceptions import ApiError
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import (
    ConfigMap,
    Namespace,
    Pod,
    Secret,
    ServiceAccount,
)
from pytest_operator.plugin import OpsTest
from tenacity import Retrying, stop_after_delay, wait_fixed

logger = logging.getLogger(__name__)

MANIFESTS_SUFFIX = "-s3"
METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
METACONTROLLER_CHARM_NAME = "metacontroller-operator"
OBJECT_STORAGE_CHARM_NAME = "minio"
OBJECT_STORAGE_CONFIG = {
    "access-key": "minio",
    "secret-key": "minio123",
    "port": "9000",
}
RESOURCE_DISPATCHER_CHARM_NAME = "resource-dispatcher"
CHARM_NAME = METADATA["name"]
NAMESPACE_FILE = "./tests/integration/namespace.yaml"
TESTING_LABELS = ["user.kubeflow.org/enabled"]
ISTIO_VERSION = "1.16/stable"
KNATIVE_VERSION = "latest/edge"
ISTIO_INGRESS_GATEWAY = "test-gateway"
METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]
CONFIGMAP_NAME = "inferenceservice-config"
EXPECTED_CONFIGMAP = yaml.safe_load(Path("./tests/integration/config-map-data.yaml").read_text())
EXPECTED_CONFIGMAP_CHANGED = yaml.safe_load(
    Path("./tests/integration/config-map-data-changed.yaml").read_text()
)
PODDEFAULTS_CRD_TEMPLATE = "./tests/integration/crds/poddefaults.yaml"

PodDefault = lightkube.generic_resource.create_namespaced_resource(
    "kubeflow.org", "v1alpha1", "PodDefault", "poddefaults"
)
TESTING_NAMESPACE_NAME = "raw-deployment"
KSERVE_WORKLOAD_CONTAINER = "kserve-container"

ISVC = lightkube.generic_resource.create_namespaced_resource(
    group="serving.kserve.io",
    version="v1beta1",
    kind="InferenceService",
    plural="inferenceservices",
    verbs=None,
)

SKLEARN_INF_SVC_YAML = yaml.safe_load(Path("./tests/integration/sklearn-iris.yaml").read_text())
SKLEARN_INF_SVC_OBJECT = lightkube.codecs.load_all_yaml(yaml.dump(SKLEARN_INF_SVC_YAML))[0]
SKLEARN_INF_SVC_NAME = SKLEARN_INF_SVC_OBJECT.metadata.name


def deploy_k8s_resources(template_files: str):
    """Deploy k8s resources from template files."""
    lightkube_client = lightkube.Client(field_manager=CHARM_NAME)
    k8s_resource_handler = KubernetesResourceHandler(
        field_manager=CHARM_NAME, template_files=template_files, context={}
    )
    lightkube.generic_resource.load_in_cluster_generic_resources(lightkube_client)
    k8s_resource_handler.apply()


def delete_all_from_yaml(yaml_text: str, lightkube_client: lightkube.Client = None):
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


def _safe_load_file_to_text(filename: str) -> str:
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


@pytest.fixture(scope="session")
def namespace(lightkube_client: lightkube.Client):
    """Create user namespace with testing label"""
    yaml_text = _safe_load_file_to_text(NAMESPACE_FILE)
    yaml_rendered = yaml.safe_load(yaml_text)
    for label in TESTING_LABELS:
        yaml_rendered["metadata"]["labels"][label] = "true"
    obj = lightkube.codecs.from_dict(yaml_rendered)
    lightkube_client.apply(obj)

    yield obj.metadata.name

    delete_all_from_yaml(yaml_text, lightkube_client)


@pytest.fixture(scope="function")
def serverless_namespace(lightkube_client):
    """Create a namespaces used for deploying inferenceservices, cleaning it up afterwards."""

    namespace_name = "serverless-namespace"
    lightkube_client.create(Namespace(metadata=ObjectMeta(name=namespace_name)))

    yield namespace_name

    try:
        lightkube_client.delete(Namespace, name=namespace_name)
    except ApiError:
        logger.warning(f"The {namespace_name} namespace could not be removed.")
        pass


@pytest.fixture(scope="session")
def lightkube_client() -> lightkube.Client:
    client = lightkube.Client(field_manager="kserve")
    return client


@pytest.mark.skip_if_deployed
@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest):
    """Build the charm-under-test and deploy it together with related charms.

    Assert on the unit status before any relations/configurations take place.
    """
    # Deploy istio-operators for ingress configuration
    await ops_test.model.deploy(
        "istio-pilot",
        channel=ISTIO_VERSION,
        config={"default-gateway": ISTIO_INGRESS_GATEWAY},
        trust=True,
    )

    await ops_test.model.deploy(
        "istio-gateway",
        application_name="istio-ingressgateway",
        channel=ISTIO_VERSION,
        config={"kind": "ingress"},
        trust=True,
    )
    await ops_test.model.integrate("istio-pilot", "istio-ingressgateway")
    await ops_test.model.wait_for_idle(
        ["istio-pilot", "istio-ingressgateway"],
        raise_on_blocked=False,
        status="active",
        timeout=90 * 10,
    )

    # build and deploy charm from local source folder
    charm = await ops_test.build_charm(".")
    resources = {
        "kserve-controller-image": METADATA["resources"]["kserve-controller-image"][
            "upstream-source"
        ]
    }
    await ops_test.model.deploy(
        charm,
        resources=resources,
        config={"deployment-mode": "rawdeployment"},
        application_name=APP_NAME,
        trust=True,
    )
    await ops_test.model.integrate("istio-pilot", APP_NAME)

    # issuing dummy update_status just to trigger an event
    async with ops_test.fast_forward():
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


@pytest.fixture()
def test_namespace(lightkube_client: lightkube.Client):
    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=15),
        stop=tenacity.stop_after_delay(30),
        reraise=True,
    )
    def create_namespace():
        lightkube_client.create(Namespace(metadata=ObjectMeta(name=TESTING_NAMESPACE_NAME)))

    create_namespace()
    yield
    lightkube_client.delete(Namespace, name=TESTING_NAMESPACE_NAME)


@pytest.mark.parametrize(
    "inference_file",
    [
        "./tests/integration/hugginface-bert.yaml",
        "./tests/integration/sklearn-iris.yaml",
        "./tests/integration/lgbserver.yaml",
        "./tests/integration/pmml-server.yaml",
        "./tests/integration/paddleserver-resnet.yaml",
        "./tests/integration/xgbserver.yaml",
        "./tests/integration/tensorflow-serving.yaml",
    ],
)
def test_inference_service_raw_deployment(
    test_namespace: None, lightkube_client: lightkube.Client, inference_file, ops_test: OpsTest
):
    """Validates that an InferenceService can be deployed."""
    # Read InferenceService example

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
        lightkube_client.create(inf_svc_object, namespace=TESTING_NAMESPACE_NAME)

    # Assert InferenceService state is Available
    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=30),
        stop=tenacity.stop_after_attempt(30),
        reraise=True,
    )
    def assert_inf_svc_state():
        inf_svc = lightkube_client.get(ISVC, inf_svc_name, namespace=TESTING_NAMESPACE_NAME)
        conditions = inf_svc.get("status", {}).get("conditions")
        logger.info(
            f"INFO: Inspecting InferenceService {inf_svc.metadata.name} in namespace {inf_svc.metadata.namespace}"
        )

        for condition in conditions:
            if condition.get("status") in ["False", "Unknown"]:
                logger.info(f"Inference service is not ready according to condition: {condition}")
                status_overall = False
                print_inf_svc_logs(lightkube_client=lightkube_client, inf_svc=inf_svc)
                break
            status_overall = True
            logger.info("Service is ready")
        assert status_overall is True

    create_inf_svc()
    assert_inf_svc_state()


async def test_logging(ops_test: OpsTest):
    """Test logging is defined in relation data bag."""
    app = ops_test.model.applications[APP_NAME]
    await assert_logging(app)


async def test_metrics_enpoint(ops_test):
    """Test metrics_endpoints are defined in relation data bag and their accessibility.
    This function gets all the metrics_endpoints from the relation data bag, checks if
    they are available from the grafana-agent-k8s charm and finally compares them with the
    ones provided to the function.
    """
    app = ops_test.model.applications[APP_NAME]
    await assert_metrics_endpoint(app, metrics_port=8080, metrics_path="/metrics")


async def test_alert_rules(ops_test):
    """Test check charm alert rules and rules defined in relation data bag."""
    app = ops_test.model.applications[APP_NAME]
    alert_rules = get_alert_rules()
    logger.info("found alert_rules: %s", alert_rules)
    await assert_alert_rules(app, alert_rules)


#    # Remove the InferenceService deployed in RawDeployment mode
#    lightkube_client.delete(
#        inference_service_resource, name=inf_svc_name, namespace=rawdeployment_mode_namespace
#    )


async def test_deploy_knative_dependencies(ops_test: OpsTest):
    """Deploy knative-operators as dependencies for serverless mode."""
    # Deploy knative for serverless mode
    namespace = ops_test.model_name

    # Deploy knative-operator
    await ops_test.model.deploy(
        "knative-operator",
        channel=KNATIVE_VERSION,
        trust=True,
    )

    # Wait for idle knative-operator before deploying knative-serving
    # due to issue https://github.com/canonical/knative-operators/issues/156
    await ops_test.model.wait_for_idle(
        ["knative-operator"],
        status="active",
        raise_on_blocked=False,
        timeout=90 * 10,
    )

    # Deploy knative-serving
    await ops_test.model.deploy(
        "knative-serving",
        channel=KNATIVE_VERSION,
        config={
            "namespace": "knative-serving",
            "istio.gateway.namespace": namespace,
            "istio.gateway.name": ISTIO_INGRESS_GATEWAY,
        },
        trust=True,
    )
    await ops_test.model.wait_for_idle(
        ["knative-serving"],
        raise_on_blocked=False,
        status="active",
        timeout=90 * 10,
    )

    # Relate kserve-controller and knative-serving
    await ops_test.model.integrate("knative-serving", APP_NAME)

    # Change deployment mode to Serverless
    await ops_test.model.applications[APP_NAME].set_config({"deployment-mode": "serverless"})

    await ops_test.model.wait_for_idle(
        [APP_NAME],
        raise_on_blocked=False,
        status="active",
        timeout=90 * 10,
    )


def test_inference_service_serverless_deployment(serverless_namespace, ops_test: OpsTest):
    """Validates that an InferenceService can be deployed."""
    # Instantiate a lightkube client
    lightkube_client = lightkube.Client()

    # Create InferenceService from example file
    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=15),
        stop=tenacity.stop_after_delay(30),
        reraise=True,
    )
    def create_inf_svc():
        lightkube_client.create(SKLEARN_INF_SVC_OBJECT, namespace=serverless_namespace)

    # Assert InferenceService state is Available
    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=15),
        stop=tenacity.stop_after_attempt(30),
        reraise=True,
    )
    def assert_inf_svc_state():
        inf_svc = lightkube_client.get(ISVC, SKLEARN_INF_SVC_NAME, namespace=serverless_namespace)
        conditions = inf_svc.get("status", {}).get("conditions")
        for condition in conditions:
            if condition.get("status") == "False":
                status_overall = False
                break
            status_overall = True
        assert status_overall is True

    create_inf_svc()
    assert_inf_svc_state()


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
    assert inferenceservice_config.data == EXPECTED_CONFIGMAP


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
    assert inferenceservice_config.data == EXPECTED_CONFIGMAP_CHANGED


async def test_relate_to_object_store(ops_test: OpsTest):
    """Test if the charm can relate to minio and stay in Active state"""
    await ops_test.model.deploy(
        OBJECT_STORAGE_CHARM_NAME,
        channel="ckf-1.7/stable",
        config=OBJECT_STORAGE_CONFIG,
        trust=True,
    )
    await ops_test.model.wait_for_idle(
        apps=[OBJECT_STORAGE_CHARM_NAME],
        status="active",
        raise_on_blocked=False,
        raise_on_error=False,
        timeout=600,
    )
    await ops_test.model.integrate(OBJECT_STORAGE_CHARM_NAME, CHARM_NAME)
    await ops_test.model.wait_for_idle(
        apps=[CHARM_NAME],
        status="active",
        raise_on_blocked=False,
        raise_on_error=False,
        timeout=600,
    )
    assert ops_test.model.applications[CHARM_NAME].units[0].workload_status == "active"


async def test_deploy_resource_dispatcher(ops_test: OpsTest):
    """
    Test if the charm can relate to resource dispatcher and stay in Active state

    We need to deploy Metacontroller and poddefaults CRD (for Resource dispatcher).
    """
    deploy_k8s_resources([PODDEFAULTS_CRD_TEMPLATE])
    await ops_test.model.deploy(
        entity_url=METACONTROLLER_CHARM_NAME,
        channel="latest/edge",
        trust=True,
    )
    await ops_test.model.wait_for_idle(
        apps=[METACONTROLLER_CHARM_NAME],
        status="active",
        raise_on_blocked=False,
        raise_on_error=False,
        timeout=120,
    )
    await ops_test.model.deploy(RESOURCE_DISPATCHER_CHARM_NAME, channel="latest/edge", trust=True)
    await ops_test.model.wait_for_idle(
        apps=[CHARM_NAME],
        status="active",
        raise_on_blocked=False,
        raise_on_error=False,
        timeout=120,
        idle_period=60,
    )

    await ops_test.model.integrate(
        f"{CHARM_NAME}:service-accounts", f"{RESOURCE_DISPATCHER_CHARM_NAME}:service-accounts"
    )
    await ops_test.model.integrate(
        f"{CHARM_NAME}:secrets", f"{RESOURCE_DISPATCHER_CHARM_NAME}:secrets"
    )

    await ops_test.model.wait_for_idle(
        apps=[RESOURCE_DISPATCHER_CHARM_NAME, CHARM_NAME],
        status="active",
        raise_on_blocked=False,
        raise_on_error=False,
        timeout=1200,
    )
    assert ops_test.model.applications[CHARM_NAME].units[0].workload_status == "active"


async def test_new_user_namespace_has_manifests(
    ops_test: OpsTest, lightkube_client: lightkube.Client, namespace: str
):
    """Create user namespace with correct label and check manifests."""
    time.sleep(30)  # sync can take up to 10 seconds for reconciliation loop to trigger
    manifests_name = f"{CHARM_NAME}{MANIFESTS_SUFFIX}"
    secret = lightkube_client.get(Secret, manifests_name, namespace=namespace)
    service_account = lightkube_client.get(ServiceAccount, manifests_name, namespace=namespace)
    assert secret.data == {
        "AWS_ACCESS_KEY_ID": base64.b64encode(
            OBJECT_STORAGE_CONFIG["access-key"].encode("utf-8")
        ).decode("utf-8"),
        "AWS_SECRET_ACCESS_KEY": base64.b64encode(
            OBJECT_STORAGE_CONFIG["secret-key"].encode("utf-8")
        ).decode("utf-8"),
    }
    assert service_account.secrets[0].name == manifests_name


RETRY_FOR_THREE_MINUTES = Retrying(
    stop=stop_after_delay(60 * 3),
    wait=wait_fixed(5),
    reraise=True,
)


async def test_inference_service_proxy_envs_configuration(
    serverless_namespace, ops_test: OpsTest, lightkube_client: lightkube.Client
):
    """Changes `http-proxy`, `https-proxy` and `no-proxy` configs and asserts that
    the InferenceService Pod is using the values from configs as environment variables."""

    # Set Proxy envs by setting the charm configs
    test_http_proxy = "my_http_proxy"
    test_https_proxy = "my_https_proxy"
    test_no_proxy = "no_proxy"

    await ops_test.model.applications["kserve-controller"].set_config(
        {"http-proxy": test_http_proxy, "https-proxy": test_https_proxy, "no-proxy": test_no_proxy}
    )

    await ops_test.model.wait_for_idle(
        ["kserve-controller"],
        status="active",
        raise_on_blocked=False,
        timeout=60 * 1,
    )

    # Create InferenceService from example file
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            lightkube_client.create(SKLEARN_INF_SVC_OBJECT, namespace=serverless_namespace)

    # Assert InferenceService Pod specifies the proxy envs for the initContainer
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            pods_list = lightkube_client.list(
                res=Pod,
                namespace=serverless_namespace,
                labels={"serving.kserve.io/inferenceservice": SKLEARN_INF_SVC_NAME},
            )
            isvc_pod = next(pods_list)
            init_env_vars = isvc_pod.spec.initContainers[0].env

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
