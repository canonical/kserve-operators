#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
from pathlib import Path

import jubilant
import pytest

from .helpers.assertions import (
    assert_gateway_programmed,
    assert_inferencepool_and_workload_resources,
    assert_llmisvc_metrics_endpoints,
    assert_no_charm_resources_left,
    assert_prediction,
    assert_route_programmed,
)
from .helpers.charm_paths import (
    resolve_charm_path,
    resolve_charm_resources,
    resolve_test_charm_path,
)
from .helpers.cluster_setup import (
    ensure_gateway,
    install_envoy_ai_gateway,
    install_envoy_gateway,
    install_gateway_api_crds,
    install_gie_crds,
    install_lws,
)
from .helpers.llmisvc_ops import apply_llmisvc_example, delete_llmisvc_example

logger = logging.getLogger(__name__)

GATEWAY_API_VERSION = "v1.4.1"
GIE_VERSION = "v1.3.0"
ENVOY_GATEWAY_VERSION = "v1.6.3"
ENVOY_AI_GATEWAY_VERSION = "v0.5.0"
LWS_VERSION = "v0.7.0"
KSERVE_NAMESPACE = "kubeflow"
GATEWAY_NAME = "kserve-ingress-gateway"
GATEWAY_NAMESPACE = "kubeflow"
CONTROLLER_APP = "kserve-controller"
LLMISVC_APP = "kserve-llmisvc"
GATEWAY_METADATA_PROVIDER_CHARM = "gateway-metadata-provider-tester"
LWS_CONTROLLER_TESTER_CHARM = "lws-controller-tester"
TEST_DATA_DIR = Path(__file__).parent / "test_data"
# Images injected into the LLMInferenceService example templates at apply time.
# They are sourced from the charms' default-custom-images.json so the tests
# exercise the same images the charms ship by default.
REPO_ROOT = Path(__file__).parent.parent.parent
KSERVE_CONTROLLER_IMAGES = json.loads(
    (REPO_ROOT / "charms/kserve-controller/src/default-custom-images.json").read_text()
)
KSERVE_LLMISVC_IMAGES = json.loads(
    (REPO_ROOT / "charms/kserve-llmisvc/src/default-custom-images.json").read_text()
)
STORAGE_INITIALIZER_IMAGE = KSERVE_CONTROLLER_IMAGES["configmap__storageInitializer"]
VLLM_IMAGE = KSERVE_LLMISVC_IMAGES["vllm"]
LLMISVC_IMAGE_CONTEXT = {
    "storage_initializer_image": STORAGE_INITIALIZER_IMAGE,
    "vllm_image": VLLM_IMAGE,
}
# LLMInferenceService example templates to deploy and run predictions against,
# as (custom-resource name, manifest template path) pairs. Each example is
# applied, verified, predicted against, and then deleted before the next one so
# the cluster's resource usage stays bounded (important on constrained CI
# runners). Add a new example by appending another (name, path) tuple.
LLMISVC_EXAMPLES = [
    (
        "test-llm-scheduler-small",
        TEST_DATA_DIR / "llmisvc_test_llm_scheduler_small.yaml.j2",
    ),
    (
        "test-llm-prefill-decode",
        TEST_DATA_DIR / "llmisvc_test_llm_prefill_decode.yaml.j2",
    ),
]


@pytest.mark.deploy
@pytest.mark.abort_on_fail
def test_setup_charms(juju: jubilant.Juju, request: pytest.FixtureRequest):
    charms_path = request.config.getoption("--charms-path")
    if not charms_path:
        raise ValueError("--charms-path is required for bundle integration tests")

    controller_charm = resolve_charm_path(charms_path=charms_path, charm_name=CONTROLLER_APP)
    llmisvc_charm = resolve_charm_path(charms_path=charms_path, charm_name=LLMISVC_APP)
    lws_charm = resolve_test_charm_path(LWS_CONTROLLER_TESTER_CHARM)
    gateway_metadata_charm = resolve_test_charm_path(GATEWAY_METADATA_PROVIDER_CHARM)
    controller_resources = resolve_charm_resources(charm_name=CONTROLLER_APP)
    llmisvc_resources = resolve_charm_resources(charm_name=LLMISVC_APP)

    for _, example_path in LLMISVC_EXAMPLES:
        if not example_path.exists():
            raise RuntimeError(f"LLMInferenceService manifest file not found: {example_path!s}")

    logger.info("Starting bundle integration test setup")
    logger.info("Installing Gateway API CRDs")
    install_gateway_api_crds(version=GATEWAY_API_VERSION)

    logger.info("Installing Gateway API Inference Extension CRDs")
    install_gie_crds(version=GIE_VERSION)

    logger.info("Installing Envoy Gateway")
    install_envoy_gateway(
        envoy_gateway_version=ENVOY_GATEWAY_VERSION,
        envoy_ai_gateway_version=ENVOY_AI_GATEWAY_VERSION,
    )

    logger.info("Installing Envoy AI Gateway")
    install_envoy_ai_gateway(envoy_ai_gateway_version=ENVOY_AI_GATEWAY_VERSION)

    logger.info("Creating Gateway resource")
    ensure_gateway(
        kserve_namespace=KSERVE_NAMESPACE,
        gateway_name=GATEWAY_NAME,
        gateway_namespace=GATEWAY_NAMESPACE,
    )

    logger.info("Installing LWS Helm chart")
    install_lws(version=LWS_VERSION)

    logger.info("Deploying lws-controller tester charm")
    juju.deploy(charm=str(lws_charm))

    logger.info("Deploying kserve-controller charm")
    juju.deploy(
        charm=str(controller_charm),
        resources=controller_resources,
        config={"deployment-mode": "standard"},
        trust=True,
    )

    logger.info("Waiting for kserve-controller application to appear")
    juju.wait(lambda status: CONTROLLER_APP in status.apps, successes=1)

    logger.info("Waiting for kserve-controller to block on missing gateway-metadata relation")
    juju.wait(lambda status: status.apps[CONTROLLER_APP].is_blocked, successes=1)

    logger.info("Deploying gateway metadata provider test charm")
    juju.deploy(charm=str(gateway_metadata_charm))

    logger.info("Waiting for gateway metadata provider application to appear")
    juju.wait(lambda status: GATEWAY_METADATA_PROVIDER_CHARM in status.apps, successes=1)

    logger.info("Relating gateway metadata provider to kserve-controller")
    juju.integrate(
        "kserve-controller:gateway-metadata",
        f"{GATEWAY_METADATA_PROVIDER_CHARM}:gateway-metadata",
    )

    logger.info("Waiting for kserve-controller and gateway metadata provider to be active")
    juju.wait(
        lambda status: status.apps[CONTROLLER_APP].is_active
        and status.apps[GATEWAY_METADATA_PROVIDER_CHARM].is_active,
        successes=1,
    )

    logger.info("Deploying kserve-llmisvc charm")
    juju.deploy(
        charm=str(llmisvc_charm),
        resources=llmisvc_resources,
        trust=True,
    )

    logger.info("Waiting for kserve-llmisvc application to appear")
    juju.wait(lambda status: LLMISVC_APP in status.apps, successes=1)

    logger.info("Relating charms")
    juju.integrate("kserve-controller:kserve-controller", "kserve-llmisvc:kserve-controller")
    juju.integrate("lws-controller-tester:lws-controller", "kserve-llmisvc:lws-controller")
    logger.info("Waiting for all charms to be active after relations")
    juju.wait(jubilant.all_active, successes=1)

    logger.info("Waiting for Gateway to be programmed")
    assert_gateway_programmed(gateway_name=GATEWAY_NAME, gateway_namespace=GATEWAY_NAMESPACE)

    logger.info("Charm setup complete")


@pytest.mark.deploy
@pytest.mark.abort_on_fail
@pytest.mark.parametrize(
    "example_name, example_path",
    LLMISVC_EXAMPLES,
    ids=[name for name, _ in LLMISVC_EXAMPLES],
)
def test_run_example(juju: jubilant.Juju, example_name: str, example_path: Path):
    logger.info("Example '%s': applying LLMInferenceService", example_name)
    apply_llmisvc_example(
        manifest_path=str(example_path),
        context=LLMISVC_IMAGE_CONTEXT,
        name=example_name,
    )

    logger.info("Example '%s': verifying generated resources", example_name)
    assert_route_programmed(name=example_name)
    assert_inferencepool_and_workload_resources(name=example_name)

    logger.info("Example '%s': verifying observability metrics endpoints", example_name)
    assert_llmisvc_metrics_endpoints(namespace=KSERVE_NAMESPACE)

    logger.info("Example '%s': testing prediction endpoint", example_name)
    assert_prediction(gateway_name=GATEWAY_NAME, name=example_name)

    logger.info("Example '%s': deleting after validation", example_name)
    delete_llmisvc_example(name=example_name)


@pytest.mark.deploy
def test_remove_charms_leaves_no_charm_resources(juju: jubilant.Juju):
    logger.info("Starting bundle cleanup test")
    logger.info("Removing charm applications from Juju model")
    juju.remove_application(LLMISVC_APP)
    juju.remove_application(GATEWAY_METADATA_PROVIDER_CHARM)
    juju.remove_application(CONTROLLER_APP)
    juju.remove_application(LWS_CONTROLLER_TESTER_CHARM)

    logger.info("Waiting for charm applications to disappear from Juju model")
    juju.wait(
        lambda status: CONTROLLER_APP not in status.apps
        and LLMISVC_APP not in status.apps
        and LWS_CONTROLLER_TESTER_CHARM not in status.apps,
        successes=1,
    )

    juju.wait(
        lambda status: GATEWAY_METADATA_PROVIDER_CHARM not in status.apps,
        successes=1,
    )

    logger.info("Verifying charm-owned resources are fully removed from cluster")
    assert_no_charm_resources_left()

    logger.info("Bundle cleanup test passed: no charm-owned resources left")
