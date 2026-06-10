#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path

import jubilant
import pytest

from .helpers.bundle_ops import (
    apply_llmisvc_example,
    assert_gateway_programmed,
    assert_inferencepool_and_workload_resources,
    assert_llmisvc_metrics_endpoints,
    assert_no_charm_resources_left,
    assert_prediction,
    assert_route_programmed,
    delete_llmisvc_example,
    ensure_gateway,
    install_envoy_ai_gateway,
    install_envoy_gateway,
    install_gateway_api_crds,
    install_gie_crds,
    install_lws,
)
from .helpers.charm_paths import (
    resolve_charm_path,
    resolve_charm_resources,
    resolve_test_charm_path,
)

GATEWAY_API_VERSION = "v1.4.1"
GIE_VERSION = "v1.3.0"
ENVOY_GATEWAY_VERSION = "v1.6.3"
ENVOY_AI_GATEWAY_VERSION = "v0.5.0"
LWS_VERSION = "v0.7.0"
KSERVE_NAMESPACE = "kubeflow"
GATEWAY_NAME = "kserve-ingress-gateway"
GATEWAY_NAMESPACE = "kubeflow"
LLMISVC_EXAMPLE_PATH = Path(__file__).parent / "test_data" / "llmisvc_test_llm_scheduler_small.yaml"
GATEWAY_METADATA_PROVIDER_CHARM = "gateway-metadata-provider-tester"
LWS_CONTROLLER_TESTER_CHARM = "lws-controller-tester"


@pytest.mark.deploy
@pytest.mark.abort_on_fail
def test_bundle_deploy_and_predict(juju: jubilant.Juju, request: pytest.FixtureRequest):
    logger = logging.getLogger(__name__)

    charms_path = request.config.getoption("--charms-path")
    if not charms_path:
        raise ValueError("--charms-path is required for bundle integration tests")

    controller_charm = resolve_charm_path(charms_path=charms_path, charm_name="kserve-controller")
    llmisvc_charm = resolve_charm_path(charms_path=charms_path, charm_name="kserve-llmisvc")
    lws_charm = resolve_test_charm_path(LWS_CONTROLLER_TESTER_CHARM)
    gateway_metadata_charm = resolve_test_charm_path(GATEWAY_METADATA_PROVIDER_CHARM)
    controller_resources = resolve_charm_resources(charm_name="kserve-controller")
    llmisvc_resources = resolve_charm_resources(charm_name="kserve-llmisvc")

    if not LLMISVC_EXAMPLE_PATH.exists():
        raise RuntimeError(f"LLMInferenceService manifest file not found: {LLMISVC_EXAMPLE_PATH!s}")

    logger.info("Starting bundle integration test")
    try:
        logger.info("Step 1: Installing Gateway API CRDs")
        install_gateway_api_crds(version=GATEWAY_API_VERSION)

        logger.info("Step 2: Installing Gateway API Inference Extension CRDs")
        install_gie_crds(version=GIE_VERSION)

        logger.info("Step 3: Installing Envoy Gateway")
        install_envoy_gateway(
            envoy_gateway_version=ENVOY_GATEWAY_VERSION,
            envoy_ai_gateway_version=ENVOY_AI_GATEWAY_VERSION,
        )

        logger.info("Step 4: Installing Envoy AI Gateway")
        install_envoy_ai_gateway(envoy_ai_gateway_version=ENVOY_AI_GATEWAY_VERSION)

        logger.info("Step 5: Creating Gateway resource")
        ensure_gateway(
            kserve_namespace=KSERVE_NAMESPACE,
            gateway_name=GATEWAY_NAME,
            gateway_namespace=GATEWAY_NAMESPACE,
        )

        logger.info("Step 6: Installing LWS Helm chart")
        install_lws(version=LWS_VERSION)

        logger.info("Step 7: Deploying lws-controller tester charm")
        juju.deploy(charm=str(lws_charm))

        logger.info("Step 8: Deploying kserve-controller charm")
        juju.deploy(
            charm=str(controller_charm),
            resources=controller_resources,
            config={"deployment-mode": "standard"},
            trust=True,
        )

        logger.info("Waiting for kserve-controller application to appear")
        juju.wait(lambda status: "kserve-controller" in status.apps, successes=1)

        logger.info("Waiting for kserve-controller to block on missing gateway-metadata relation")
        juju.wait(lambda status: status.apps["kserve-controller"].is_blocked, successes=1)

        logger.info("Step 9: Deploying gateway metadata provider test charm")
        juju.deploy(charm=str(gateway_metadata_charm))

        logger.info("Waiting for gateway metadata provider application to appear")
        juju.wait(lambda status: GATEWAY_METADATA_PROVIDER_CHARM in status.apps, successes=1)

        logger.info("Step 10: Relating gateway metadata provider to kserve-controller")
        juju.integrate(
            "kserve-controller:gateway-metadata",
            f"{GATEWAY_METADATA_PROVIDER_CHARM}:gateway-metadata",
        )

        logger.info("Waiting for kserve-controller and gateway metadata provider to be active")
        juju.wait(
            lambda status: status.apps["kserve-controller"].is_active
            and status.apps[GATEWAY_METADATA_PROVIDER_CHARM].is_active,
            successes=1,
        )

        logger.info("Step 11: Deploying kserve-llmisvc charm")
        juju.deploy(
            charm=str(llmisvc_charm),
            resources=llmisvc_resources,
            trust=True,
        )

        logger.info("Waiting for kserve-llmisvc application to appear")
        juju.wait(lambda status: "kserve-llmisvc" in status.apps, successes=1)

        logger.info("Step 12: Relating charms")
        juju.integrate("kserve-controller:kserve-controller", "kserve-llmisvc:kserve-controller")
        juju.integrate("lws-controller-tester:lws-controller", "kserve-llmisvc:lws-controller")
        logger.info("Waiting for all charms to be active after relations")
        juju.wait(jubilant.all_active, successes=1)

        logger.info("Step 13: Waiting for Gateway to be programmed")
        assert_gateway_programmed(gateway_name=GATEWAY_NAME, gateway_namespace=GATEWAY_NAMESPACE)

        logger.info("Step 14: Applying LLMInferenceService example")
        apply_llmisvc_example(manifest_path=str(LLMISVC_EXAMPLE_PATH))

        logger.info("Step 15: Verifying generated resources")
        assert_route_programmed()
        assert_inferencepool_and_workload_resources()

        logger.info("Step 16: Verifying llmisvc observability metrics endpoints")
        assert_llmisvc_metrics_endpoints(namespace=KSERVE_NAMESPACE)

        logger.info("Step 17: Testing prediction endpoint")
        assert_prediction(gateway_name=GATEWAY_NAME)

        logger.info("All bundle integration tests passed!")
    except Exception:
        raise


@pytest.mark.deploy
def test_bundle_remove_charms_leaves_no_charm_resources(juju: jubilant.Juju):
    logger = logging.getLogger(__name__)

    logger.info("Starting bundle cleanup test")
    try:
        logger.info("Deleting LLMInferenceService example before removing charms")
        # Must happen while kserve-controller is still up so the controller can
        # clear the LLMInferenceService finalizer; otherwise the custom resource
        # (and its CRD) get stuck terminating and leak charm-owned resources.
        delete_llmisvc_example(manifest_path=str(LLMISVC_EXAMPLE_PATH))

        logger.info("Removing charm applications from Juju model")
        juju.remove_application("kserve-llmisvc", force=True)
        juju.remove_application(GATEWAY_METADATA_PROVIDER_CHARM, force=True)
        juju.remove_application("kserve-controller", force=True)
        juju.remove_application(LWS_CONTROLLER_TESTER_CHARM, force=True)

        logger.info("Waiting for charm applications to disappear from Juju model")
        juju.wait(
            lambda status: "kserve-controller" not in status.apps
            and "kserve-llmisvc" not in status.apps
            and LWS_CONTROLLER_TESTER_CHARM not in status.apps,
            # gateway metadata provider is a test harness charm and must be removed too
            # to avoid leaking applications between runs.
            successes=1,
        )

        juju.wait(
            lambda status: GATEWAY_METADATA_PROVIDER_CHARM not in status.apps,
            successes=1,
        )

        logger.info("Verifying charm-owned resources are fully removed from cluster")
        assert_no_charm_resources_left()

        logger.info("Bundle cleanup test passed: no charm-owned resources left")
    except Exception:
        raise
