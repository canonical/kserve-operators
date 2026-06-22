#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the kserve-controller `s3-credentials` relation.

These tests deploy kserve-controller together with an s3-integrator charm and
assert that the S3 credentials provided over the `s3-credentials` relation are
rendered into the Secret and ServiceAccount that are dispatched, via the
resource-dispatcher, into user namespaces.
"""

import base64
import logging

import lightkube
import pytest
from charmed_kubeflow_chisme.testing.s3_integration import (
    deploy_and_assert_s3_integrator,
    host_ip,
)
from pytest_operator.plugin import OpsTest

from tests.integration.charms_dependencies import (
    ISTIO_GATEWAY,
    ISTIO_PILOT,
    KNATIVE_OPERATOR,
    KNATIVE_SERVING,
    METACONTROLLER_OPERATOR,
    RESOURCE_DISPATCHER,
    S3_INTEGRATOR,
)
from tests.integration.constants import (
    APP_NAME,
    MANIFESTS_SUFFIX,
    METADATA,
    PODDEFAULTS_CRD_TEMPLATE,
)
from tests.integration.utils import (
    deploy_k8s_resources,
    get_k8s_secret,
    get_k8s_service_account,
)

logger = logging.getLogger(__name__)

ISTIO_INGRESS_GATEWAY = "test-gateway"
ISTIO_GATEWAY_APP_NAME = "istio-ingressgateway"


@pytest.mark.skip_if_deployed
@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest, request):
    """Build and deploy kserve-controller with the istio/knative dependencies.

    Assert that the charm reaches Active before any storage relation is added.
    """
    # Deploy istio-operators for ingress configuration
    await ops_test.model.deploy(
        ISTIO_PILOT.charm,
        channel=ISTIO_PILOT.channel,
        config={"default-gateway": ISTIO_INGRESS_GATEWAY},
        trust=ISTIO_PILOT.trust,
    )

    await ops_test.model.deploy(
        ISTIO_GATEWAY.charm,
        application_name=ISTIO_GATEWAY_APP_NAME,
        channel=ISTIO_GATEWAY.channel,
        config=ISTIO_GATEWAY.config,
        trust=ISTIO_GATEWAY.trust,
    )
    await ops_test.model.integrate(ISTIO_PILOT.charm, ISTIO_GATEWAY_APP_NAME)
    await ops_test.model.wait_for_idle(
        [ISTIO_PILOT.charm, ISTIO_GATEWAY_APP_NAME],
        raise_on_blocked=False,
        status="active",
        timeout=90 * 10,
    )

    # Deploy knative-operator
    await ops_test.model.deploy(
        KNATIVE_OPERATOR.charm,
        channel=KNATIVE_OPERATOR.channel,
        trust=KNATIVE_OPERATOR.trust,
    )

    # Wait for idle knative-operator before deploying knative-serving
    # due to issue https://github.com/canonical/knative-operators/issues/156
    await ops_test.model.wait_for_idle(
        [KNATIVE_OPERATOR.charm],
        status="active",
        raise_on_blocked=False,
        timeout=90 * 10,
    )

    # Deploy knative-serving
    await ops_test.model.deploy(
        KNATIVE_SERVING.charm,
        channel=KNATIVE_SERVING.channel,
        config={
            "istio.gateway.namespace": ops_test.model_name,
            "istio.gateway.name": ISTIO_INGRESS_GATEWAY,
        },
        trust=KNATIVE_SERVING.trust,
    )
    await ops_test.model.wait_for_idle(
        [KNATIVE_SERVING.charm],
        raise_on_blocked=False,
        status="active",
        timeout=90 * 10,
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
        application_name=APP_NAME,
        trust=True,
    )

    await ops_test.model.integrate(ISTIO_PILOT.charm, APP_NAME)
    # Relate kserve-controller and knative-serving
    await ops_test.model.integrate(KNATIVE_SERVING.charm, APP_NAME)

    # issuing dummy update_status just to trigger an event
    async with ops_test.fast_forward(fast_interval="60s"):
        await ops_test.model.wait_for_idle(
            apps=[APP_NAME],
            status="active",
            raise_on_blocked=False,
            timeout=1000,
        )
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"


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
    by s3-integrator, which is backed by a microceph (https) endpoint.
    """
    logger.info("Checking the created secret in the user namespace.")
    manifests_name = f"{APP_NAME}{MANIFESTS_SUFFIX}"
    secret = get_k8s_secret(manifests_name, test_namespace, lightkube_client)
    service_account = get_k8s_service_account(manifests_name, test_namespace, lightkube_client)

    # The microceph endpoint used by s3-integrator is https://{host_ip}, so the
    # rendered annotations expose the host as the s3-endpoint and use https.
    annotations = secret.metadata.annotations
    assert annotations["serving.kserve.io/s3-endpoint"] == host_ip()
    assert annotations["serving.kserve.io/s3-usehttps"] == "1"
    assert annotations["serving.kserve.io/s3-useanoncredential"] == "false"
    assert annotations["serving.kserve.io/s3-region"]

    # The credentials are generated by microceph and are not known ahead of time,
    # so assert that they are present and non-empty rather than matching values.
    assert base64.b64decode(secret.data["AWS_ACCESS_KEY_ID"])
    assert base64.b64decode(secret.data["AWS_SECRET_ACCESS_KEY"])

    assert service_account.secrets[0].name == manifests_name


async def test_remove_application(ops_test: OpsTest):
    """Test that the application can be removed successfully."""
    await ops_test.model.remove_application(app_name=APP_NAME, block_until_done=True)
    assert APP_NAME not in ops_test.model.applications
