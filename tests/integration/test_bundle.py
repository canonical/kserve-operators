#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import os
from pathlib import Path

import jubilant
import pytest

from .helpers.assertions import (
    assert_inferencepool_and_workload_resources,
    assert_llmisvc_metrics_endpoints,
    assert_no_charm_resources_left,
    assert_prediction,
    assert_route_programmed,
)
from .helpers.charm_paths import resolve_charm_path, resolve_charm_resources
from .helpers.charms_dependencies import (
    ENVOY_AI_CONTROLLER,
    ENVOY_CONTROLLER,
    ENVOY_INGRESS,
    SELF_SIGNED_CERTIFICATES,
)
from .helpers.llmisvc_ops import apply_llmisvc_example, delete_llmisvc_example

logger = logging.getLogger(__name__)
# Quiet jubilant's very verbose per-poll wait logging during the long waits.
logging.getLogger("jubilant.wait").setLevel("WARNING")

CONTROLLER_APP = "kserve-controller"
LLMISVC_APP = "kserve-llmisvc"
LWS_APP = "lws-controller"
# App names for the Charmhub dependencies (deploy coordinates live in
# helpers/charms_dependencies.py). envoy-ingress-k8s creates the Gateway and
# provides the gateway-metadata relation to kserve-controller.
ENVOY_CONTROLLER_APP = ENVOY_CONTROLLER.charm
ENVOY_AI_CONTROLLER_APP = ENVOY_AI_CONTROLLER.charm
ENVOY_INGRESS_APP = ENVOY_INGRESS.charm
CERTIFICATES_APP = SELF_SIGNED_CERTIFICATES.charm
GATEWAY_NAME = ENVOY_INGRESS_APP
TEST_DATA_DIR = Path(__file__).parent / "test_data"
# Images injected into the example manifests, sourced from the charms'
# default-custom-images.json so tests use the images the charms ship.
REPO_ROOT = Path(__file__).parent.parent.parent
KSERVE_CONTROLLER_IMAGES = json.loads(
    (REPO_ROOT / "charms/kserve-controller/src/default-custom-images.json").read_text()
)
KSERVE_LLMISVC_IMAGES = json.loads(
    (REPO_ROOT / "charms/kserve-llmisvc/src/default-custom-images.json").read_text()
)
STORAGE_INITIALIZER_IMAGE = KSERVE_CONTROLLER_IMAGES["configmap__storageInitializer"]
VLLM_IMAGE = KSERVE_LLMISVC_IMAGES["vllm"]
# The test model lives in a Canonical S3 bucket (avoids the flaky HF CDN). The
# AWS credentials are supplied via the environment (local export or CI secrets).
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
MODEL_S3_URI = os.environ.get("TEST_MODEL_S3_URI", "s3://charmed-kubeflow-llm-storage/pythia-70m")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")


# Fail fast (rather than skip) if the S3 credentials for the test model are missing.
@pytest.fixture(scope="session", autouse=True)
def require_aws_credentials():
    if not (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY):
        pytest.fail(
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set to fetch the test "
            "model from S3; export them locally or provide them via CI secrets.",
            pytrace=False,
        )


LLMISVC_IMAGE_CONTEXT = {
    "storage_initializer_image": STORAGE_INITIALIZER_IMAGE,
    "vllm_image": VLLM_IMAGE,
    "model_s3_uri": MODEL_S3_URI,
    "aws_access_key_id": AWS_ACCESS_KEY_ID,
    "aws_secret_access_key": AWS_SECRET_ACCESS_KEY,
    "aws_region": AWS_REGION,
    "s3_endpoint": os.environ.get("S3_ENDPOINT", f"s3.{AWS_REGION}.amazonaws.com"),
}
# (name, manifest template) pairs. Each example is applied, verified, predicted
# against, then deleted before the next so cluster usage stays bounded.
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


@pytest.mark.abort_on_fail
def test_setup_charms(juju: jubilant.Juju, request: pytest.FixtureRequest):
    charms_path = request.config.getoption("--charms-path")
    if not charms_path:
        raise ValueError("--charms-path is required for bundle integration tests")

    controller_charm = resolve_charm_path(charms_path=charms_path, charm_name=CONTROLLER_APP)
    llmisvc_charm = resolve_charm_path(charms_path=charms_path, charm_name=LLMISVC_APP)
    lws_charm = resolve_charm_path(charms_path=charms_path, charm_name=LWS_APP)
    controller_resources = resolve_charm_resources(charm_name=CONTROLLER_APP)
    llmisvc_resources = resolve_charm_resources(charm_name=LLMISVC_APP)
    lws_resources = resolve_charm_resources(charm_name=LWS_APP)

    for _, example_path in LLMISVC_EXAMPLES:
        if not example_path.exists():
            raise RuntimeError(f"LLMInferenceService manifest file not found: {example_path!s}")

    logger.info("Starting bundle integration test setup")

    logger.info("Deploying Envoy gateway charm stack")
    for dep in (ENVOY_CONTROLLER, ENVOY_AI_CONTROLLER, ENVOY_INGRESS, SELF_SIGNED_CERTIFICATES):
        juju.deploy(dep.charm, channel=dep.channel, trust=dep.trust, config=dep.config)

    logger.info("Relating Envoy charms")
    juju.integrate(ENVOY_AI_CONTROLLER_APP, CERTIFICATES_APP)
    juju.integrate(ENVOY_CONTROLLER_APP, ENVOY_AI_CONTROLLER_APP)

    logger.info("Deploying lws-controller charm")
    juju.deploy(
        charm=str(lws_charm),
        resources=lws_resources,
        trust=True,
    )

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

    logger.info("Relating kserve-controller to the Envoy gateway metadata provider")
    juju.integrate(
        f"{CONTROLLER_APP}:gateway-metadata",
        f"{ENVOY_INGRESS_APP}:gateway-metadata",
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
    juju.integrate("lws-controller:lws-controller", "kserve-llmisvc:lws-controller")

    logger.info("Waiting for all charms to be active after relations")
    juju.wait(jubilant.all_active, successes=1)

    logger.info("Charm setup complete")


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
    assert_llmisvc_metrics_endpoints(namespace=juju.model)

    logger.info("Example '%s': testing prediction endpoint", example_name)
    assert_prediction(gateway_name=GATEWAY_NAME, gateway_namespace=juju.model, name=example_name)

    logger.info("Example '%s': deleting after validation", example_name)
    delete_llmisvc_example(name=example_name)


def test_remove_charms_leaves_no_charm_resources(juju: jubilant.Juju):
    logger.info("Starting bundle cleanup test")
    logger.info("Removing charm applications from Juju model")
    juju.remove_application(LLMISVC_APP)
    juju.remove_application(CONTROLLER_APP)
    juju.remove_application(LWS_APP)
    for envoy_app in (
        ENVOY_CONTROLLER_APP,
        ENVOY_AI_CONTROLLER_APP,
        ENVOY_INGRESS_APP,
        CERTIFICATES_APP,
    ):
        juju.remove_application(envoy_app)

    logger.info("Waiting for kserve charm applications to disappear from Juju model")
    juju.wait(
        lambda status: CONTROLLER_APP not in status.apps
        and LLMISVC_APP not in status.apps
        and LWS_APP not in status.apps,
        successes=1,
    )

    logger.info("Verifying charm-owned resources are fully removed from cluster")
    assert_no_charm_resources_left()

    logger.info("Bundle cleanup test passed: no charm-owned resources left")
