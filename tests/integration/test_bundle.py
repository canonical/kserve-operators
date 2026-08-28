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
    assert_llminferenceservice_absent,
    assert_llmisvc_metrics_endpoints,
    assert_no_charm_resources_left,
    assert_prediction,
    assert_route_programmed,
    assert_secret_absent,
)
from .helpers.charm_paths import resolve_charm_path
from .helpers.charms_dependencies import (
    ENVOY_AI_CONTROLLER,
    ENVOY_CONTROLLER,
    ENVOY_INGRESS,
    SELF_SIGNED_CERTIFICATES,
)
from .helpers.constants import CONTROLLER_APP_NAME as CONTROLLER_APP
from .helpers.constants import LLMISVC_APP_NAME as LLMISVC_APP
from .helpers.constants import LLMISVC_GPU_MODEL_NAME
from .helpers.constants import LWS_APP_NAME as LWS_APP
from .helpers.deploy import deploy_serving_stack
from .helpers.llmisvc_ops import apply_llmisvc_example, delete_llmisvc_example

logger = logging.getLogger(__name__)
# Quiet jubilant's very verbose per-poll wait logging during the long waits.
logging.getLogger("jubilant.wait").setLevel("WARNING")

# App names for the Charmhub dependencies (deploy coordinates live in
# helpers/charms_dependencies.py). envoy-ingress-k8s creates the Gateway and
# provides the gateway-metadata relation to kserve-controller.
ENVOY_CONTROLLER_APP = ENVOY_CONTROLLER.charm
ENVOY_AI_CONTROLLER_APP = ENVOY_AI_CONTROLLER.charm
ENVOY_INGRESS_APP = ENVOY_INGRESS.charm
CERTIFICATES_APP = SELF_SIGNED_CERTIFICATES.charm
GATEWAY_NAME = ENVOY_INGRESS_APP
# The llm-integrator charm renders a single LLMInferenceService from config. It
# supports hf:// (public model, no token) and s3:// (credentials supplied via an
# s3-integrator relation) model URIs.
LLM_INTEGRATOR_APP = "llm-integrator"
LLM_INTEGRATOR_MODEL_URI = "hf://EleutherAI/pythia-70m"
LLM_INTEGRATOR_MODEL_NAME = "EleutherAI/pythia-70m"
# s3-integrator supplies the bucket credentials for an s3:// model URI. The
# 2/edge track (matching kserve-controller) takes credentials via a Juju secret.
S3_INTEGRATOR_APP = "s3-integrator"
S3_INTEGRATOR_CHANNEL = "2/edge"
# Juju user-secret label holding the S3 access/secret keys handed to
# s3-integrator. Distinct from the K8s Secret the charm renders for the
# workload (that one is named ``{app}-s3-creds`` and asserted on cleanup).
S3_CREDS_JUJU_SECRET_LABEL = "s3-creds"
# Name of the K8s Secret the llm-integrator charm creates for s3:// models
# (``{app.name}-s3-creds``); asserted absent after the charm is removed.
LLM_INTEGRATOR_S3_SECRET = f"{LLM_INTEGRATOR_APP}-s3-creds"
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
VLLM_GPU_IMAGE = KSERVE_LLMISVC_IMAGES["vllm_gpu"]
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
# GPU example only needs the storage-initializer and vLLM images; the model is
# pulled directly via hf:// rather than from the S3 test bucket.
GPU_IMAGE_CONTEXT = {
    "storage_initializer_image": STORAGE_INITIALIZER_IMAGE,
    "vllm_image": VLLM_GPU_IMAGE,
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
# GPU example, run separately from LLMISVC_EXAMPLES since it requires GPU
# hardware and is gated behind the 'gpu' marker / --run-gpu-tests option.
GPU_EXAMPLE = (
    "test-llm-scheduler-small-gpu",
    TEST_DATA_DIR / "llmisvc_test_llm_scheduler_small_gpu.yaml.j2",
)


@pytest.mark.abort_on_fail
def test_setup_charms(juju: jubilant.Juju, request: pytest.FixtureRequest):
    charms_path = request.config.getoption("--charms-path")
    if not charms_path:
        raise ValueError("--charms-path is required for bundle integration tests")

    for _, example_path in LLMISVC_EXAMPLES:
        if not example_path.exists():
            raise RuntimeError(f"LLMInferenceService manifest file not found: {example_path!s}")
    if not GPU_EXAMPLE[1].exists():
        raise RuntimeError(f"LLMInferenceService manifest file not found: {GPU_EXAMPLE[1]!s}")

    logger.info("Starting bundle integration test setup")
    deploy_serving_stack(juju, charms_path)
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


@pytest.mark.abort_on_fail
@pytest.mark.gpu
def test_run_gpu_example(juju: jubilant.Juju):
    example_name, example_path = GPU_EXAMPLE
    logger.info("Example '%s': applying LLMInferenceService", example_name)
    apply_llmisvc_example(
        manifest_path=str(example_path),
        context=GPU_IMAGE_CONTEXT,
        name=example_name,
    )

    logger.info("Example '%s': verifying generated resources", example_name)
    assert_route_programmed(name=example_name)
    assert_inferencepool_and_workload_resources(name=example_name)

    logger.info("Example '%s': verifying observability metrics endpoints", example_name)
    assert_llmisvc_metrics_endpoints(namespace=juju.model)

    logger.info("Example '%s': testing prediction endpoint", example_name)
    assert_prediction(
        gateway_name=GATEWAY_NAME,
        gateway_namespace=juju.model,
        name=example_name,
        model=LLMISVC_GPU_MODEL_NAME,
    )

    logger.info("Example '%s': deleting after validation", example_name)
    delete_llmisvc_example(name=example_name)


@pytest.mark.abort_on_fail
def test_deploy_llm_via_charm(juju: jubilant.Juju, request: pytest.FixtureRequest):
    charms_path = request.config.getoption("--charms-path")
    if not charms_path:
        raise ValueError("--charms-path is required for bundle integration tests")

    llm_integrator_charm = resolve_charm_path(
        charms_path=charms_path, charm_name=LLM_INTEGRATOR_APP
    )

    logger.info("Deploying llm-integrator charm")
    juju.deploy(
        charm=str(llm_integrator_charm),
        config={
            "model-uri": LLM_INTEGRATOR_MODEL_URI,
            "runtime-image": VLLM_IMAGE,
        },
        trust=True,
    )

    logger.info("Waiting for llm-integrator to block on missing kserve-llmisvc relation")
    juju.wait(lambda status: status.apps[LLM_INTEGRATOR_APP].is_blocked, successes=1)

    logger.info("Relating llm-integrator to kserve-llmisvc")
    juju.integrate(f"{LLM_INTEGRATOR_APP}:kserve-llmisvc", f"{LLMISVC_APP}:kserve-llmisvc")

    logger.info("Waiting for llm-integrator to become active (LLMInferenceService Ready)")
    juju.wait(lambda status: status.apps[LLM_INTEGRATOR_APP].is_active, successes=1)

    logger.info("Verifying charm-created LLMInferenceService resources")
    assert_route_programmed(name=LLM_INTEGRATOR_APP, namespace=juju.model)
    assert_inferencepool_and_workload_resources(name=LLM_INTEGRATOR_APP, namespace=juju.model)

    logger.info("Testing prediction against charm-created LLMInferenceService")
    assert_prediction(
        gateway_name=GATEWAY_NAME,
        gateway_namespace=juju.model,
        name=LLM_INTEGRATOR_APP,
        model=LLM_INTEGRATOR_MODEL_NAME,
        namespace=juju.model,
    )

    logger.info("Removing llm-integrator charm and verifying its LLMInferenceService is cleaned up")
    juju.remove_application(LLM_INTEGRATOR_APP)
    juju.wait(lambda status: LLM_INTEGRATOR_APP not in status.apps, successes=1)
    assert_llminferenceservice_absent(name=LLM_INTEGRATOR_APP, namespace=juju.model)


@pytest.mark.abort_on_fail
def test_deploy_llm_via_charm_s3(juju: jubilant.Juju, request: pytest.FixtureRequest):
    charms_path = request.config.getoption("--charms-path")
    if not charms_path:
        raise ValueError("--charms-path is required for bundle integration tests")

    llm_integrator_charm = resolve_charm_path(
        charms_path=charms_path, charm_name=LLM_INTEGRATOR_APP
    )
    bucket = MODEL_S3_URI.removeprefix("s3://").split("/", 1)[0]

    logger.info("Deploying s3-integrator and providing S3 credentials via a Juju secret")
    juju.deploy(
        S3_INTEGRATOR_APP,
        channel=S3_INTEGRATOR_CHANNEL,
        config={
            "endpoint": f"https://{LLMISVC_IMAGE_CONTEXT['s3_endpoint']}",
            "region": AWS_REGION,
            "bucket": bucket,
        },
    )
    secret_uri = juju.cli(
        "add-secret",
        S3_CREDS_JUJU_SECRET_LABEL,
        f"access-key={AWS_ACCESS_KEY_ID}",
        f"secret-key={AWS_SECRET_ACCESS_KEY}",
    ).strip()
    juju.cli("grant-secret", S3_CREDS_JUJU_SECRET_LABEL, S3_INTEGRATOR_APP)
    juju.config(S3_INTEGRATOR_APP, {"credentials": secret_uri})
    juju.wait(lambda status: status.apps[S3_INTEGRATOR_APP].is_active, timeout=600)

    logger.info("Deploying llm-integrator with an s3:// model URI")
    juju.deploy(
        charm=str(llm_integrator_charm),
        config={
            "model-uri": MODEL_S3_URI,
            "model-name": LLM_INTEGRATOR_MODEL_NAME,
            "runtime-image": VLLM_IMAGE,
            "storage-initializer-image": STORAGE_INITIALIZER_IMAGE,
        },
        trust=True,
    )

    logger.info("Waiting for llm-integrator to block on the missing kserve-llmisvc relation")
    juju.wait(lambda status: status.apps[LLM_INTEGRATOR_APP].is_blocked, successes=1)

    logger.info("Relating llm-integrator to kserve-llmisvc")
    juju.integrate(f"{LLM_INTEGRATOR_APP}:kserve-llmisvc", f"{LLMISVC_APP}:kserve-llmisvc")

    logger.info("llm-integrator stays blocked until the s3-credentials relation is added")
    juju.wait(lambda status: status.apps[LLM_INTEGRATOR_APP].is_blocked, successes=1)

    logger.info("Relating llm-integrator to s3-integrator")
    juju.integrate(f"{LLM_INTEGRATOR_APP}:s3-credentials", f"{S3_INTEGRATOR_APP}:s3-credentials")

    logger.info("Waiting for llm-integrator to become active (LLMInferenceService Ready)")
    juju.wait(lambda status: status.apps[LLM_INTEGRATOR_APP].is_active, successes=1)

    logger.info("Verifying charm-created LLMInferenceService resources")
    assert_route_programmed(name=LLM_INTEGRATOR_APP, namespace=juju.model)
    assert_inferencepool_and_workload_resources(name=LLM_INTEGRATOR_APP, namespace=juju.model)

    logger.info("Testing prediction against the s3-backed LLMInferenceService")
    assert_prediction(
        gateway_name=GATEWAY_NAME,
        gateway_namespace=juju.model,
        name=LLM_INTEGRATOR_APP,
        model=LLM_INTEGRATOR_MODEL_NAME,
        namespace=juju.model,
    )

    logger.info("Removing llm-integrator and s3-integrator; verifying cleanup")
    juju.remove_application(LLM_INTEGRATOR_APP)
    juju.wait(lambda status: LLM_INTEGRATOR_APP not in status.apps, successes=1)
    assert_llminferenceservice_absent(name=LLM_INTEGRATOR_APP, namespace=juju.model)
    assert_secret_absent(name=LLM_INTEGRATOR_S3_SECRET, namespace=juju.model)
    juju.remove_application(S3_INTEGRATOR_APP)
    juju.wait(lambda status: S3_INTEGRATOR_APP not in status.apps, successes=1)


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
