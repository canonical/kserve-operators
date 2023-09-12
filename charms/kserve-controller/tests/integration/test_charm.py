#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.


import logging
from pathlib import Path

import lightkube
import lightkube.codecs
import lightkube.generic_resource
import pytest
import tenacity
import yaml
from lightkube.core.exceptions import ApiError
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Namespace
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

ISTIO_VERSION = "1.16/stable"
KNATIVE_VERSION = "latest/edge"
ISTIO_INGRESS_GATEWAY = "test-gateway"
METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]


@pytest.fixture
def cleanup_namespaces_after_execution(request):
    """Removes the namespaces used for deploying inferenceservices."""
    yield
    try:
        lightkube_client = lightkube.Client()
        lightkube_client.delete(Namespace, name=request.param)
    except ApiError:
        logger.warning(f"The {request.param} namespace could not be removed.")
        pass


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
    await ops_test.model.add_relation("istio-pilot", "istio-ingressgateway")
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
        ],
        "kube-rbac-proxy-image": METADATA["resources"]["kube-rbac-proxy-image"]["upstream-source"],
    }
    await ops_test.model.deploy(charm, resources=resources, application_name=APP_NAME, trust=True)
    await ops_test.model.add_relation("istio-pilot", "kserve-controller")

    # issuing dummy update_status just to trigger an event
    async with ops_test.fast_forward():
        await ops_test.model.wait_for_idle(
            apps=[APP_NAME],
            status="active",
            raise_on_blocked=False,
            timeout=1000,
        )
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"


@pytest.mark.parametrize("cleanup_namespaces_after_execution", ["raw-namespace"], indirect=True)
def test_inference_service_raw_deployment(cleanup_namespaces_after_execution, ops_test: OpsTest):
    """Validates that an InferenceService can be deployed."""
    # Instantiate a lightkube client
    lightkube_client = lightkube.Client()

    # Read InferenceService example and create namespaced resource
    inference_service_resource = lightkube.generic_resource.create_namespaced_resource(
        group="serving.kserve.io",
        version="v1beta1",
        kind="InferenceService",
        plural="inferenceservices",
        verbs=None,
    )
    inf_svc_yaml = yaml.safe_load(Path("./tests/integration/sklearn-iris.yaml").read_text())
    inf_svc_object = lightkube.codecs.load_all_yaml(yaml.dump(inf_svc_yaml))[0]
    inf_svc_name = inf_svc_object.metadata.name
    rawdeployment_mode_namespace = "raw-namespace"

    # Create RawDeployment namespace
    lightkube_client.create(Namespace(metadata=ObjectMeta(name=rawdeployment_mode_namespace)))

    # Create InferenceService from example file
    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=15),
        stop=tenacity.stop_after_delay(30),
        reraise=True,
    )
    def create_inf_svc():
        lightkube_client.create(inf_svc_object, namespace=rawdeployment_mode_namespace)

    # Assert InferenceService state is Available
    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=15),
        stop=tenacity.stop_after_attempt(30),
        reraise=True,
    )
    def assert_inf_svc_state():
        inf_svc = lightkube_client.get(
            inference_service_resource, inf_svc_name, namespace=rawdeployment_mode_namespace
        )
        conditions = inf_svc.get("status", {}).get("conditions")
        for condition in conditions:
            if condition.get("status") == "False":
                status_overall = False
                break
            status_overall = True
        assert status_overall is True

    create_inf_svc()
    assert_inf_svc_state()


#    # Remove the InferenceService deployed in RawDeployment mode
#    lightkube_client.delete(
#        inference_service_resource, name=inf_svc_name, namespace=rawdeployment_mode_namespace
#    )


async def test_deploy_knative_dependencies(ops_test: OpsTest):
    """Deploy knative-operators as dependencies for serverless mode."""
    # Deploy knative for serverless mode
    namespace = ops_test.model_name

    # Deploy knative-operators
    await ops_test.model.deploy(
        "knative-operator",
        channel=KNATIVE_VERSION,
        trust=True,
    )
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
        ["knative-operator", "knative-serving"],
        raise_on_blocked=False,
        status="active",
        timeout=90 * 10,
    )

    # Relate kserve-controller and knative-serving
    await ops_test.model.add_relation("knative-serving", "kserve-controller")

    # Change deployment mode to Serverless
    await ops_test.model.applications["kserve-controller"].set_config(
        {"deployment-mode": "serverless"}
    )

    await ops_test.model.wait_for_idle(
        ["kserve-controller"],
        raise_on_blocked=False,
        status="active",
        timeout=90 * 10,
    )


@pytest.mark.parametrize(
    "cleanup_namespaces_after_execution, namespace, inference_service_yaml",
    [
        (
            "serverless-namespace",
            "serverless-namespace",
            "./tests/integration/sklearn-iris.yaml",
        ),
        (
            "serverless-mlserver-runtime",
            "serverless-mlserver-runtime",
            "./tests/integration/mlserver-sklearn-iris.yaml",
        ),
    ],
    indirect=["cleanup_namespaces_after_execution"],
)
def test_inference_service_serverless_deployment(
    cleanup_namespaces_after_execution, namespace, inference_service_yaml, ops_test: OpsTest
):
    """Validates that an InferenceService can be deployed."""
    # Instantiate a lightkube client
    lightkube_client = lightkube.Client()

    # Read InferenceService example and create namespaced resource
    inference_service_resource = lightkube.generic_resource.create_namespaced_resource(
        group="serving.kserve.io",
        version="v1beta1",
        kind="InferenceService",
        plural="inferenceservices",
        verbs=None,
    )
    inf_svc_yaml = yaml.safe_load(Path(inference_service_yaml).read_text())
    inf_svc_object = lightkube.codecs.load_all_yaml(yaml.dump(inf_svc_yaml))[0]
    inf_svc_name = inf_svc_object.metadata.name
    serverless_mode_namespace = namespace

    # Create Serverless namespace
    lightkube_client.create(Namespace(metadata=ObjectMeta(name=serverless_mode_namespace)))

    # Create InferenceService from example file
    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=15),
        stop=tenacity.stop_after_delay(30),
        reraise=True,
    )
    def create_inf_svc():
        lightkube_client.create(inf_svc_object, namespace=serverless_mode_namespace)

    # Assert InferenceService state is Available
    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=15),
        stop=tenacity.stop_after_attempt(30),
        reraise=True,
    )
    def assert_inf_svc_state():
        inf_svc = lightkube_client.get(
            inference_service_resource, inf_svc_name, namespace=serverless_mode_namespace
        )
        conditions = inf_svc.get("status", {}).get("conditions")
        for condition in conditions:
            if condition.get("status") == "False":
                status_overall = False
                break
            status_overall = True
        assert status_overall is True

    create_inf_svc()
    assert_inf_svc_state()
