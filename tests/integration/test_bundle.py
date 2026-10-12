#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
from pathlib import Path

import jubilant
import pytest

from .helpers.assertions import (
    assert_llmisvc_metrics_endpoints,
    assert_llmisvc_serving,
    assert_no_charm_resources_left,
    assert_workload_requests_gpus,
)
from .helpers.charms_dependencies import (
    ENVOY_AI_CONTROLLER,
    ENVOY_CONTROLLER,
    ENVOY_INGRESS,
    LWS_CONTROLLER,
    S3_INTEGRATOR,
    SELF_SIGNED_CERTIFICATES,
)
from .helpers.constants import CONTROLLER_APP_NAME as CONTROLLER_APP
from .helpers.constants import LLM_INTEGRATOR_APP_NAME as LLM_INTEGRATOR_APP
from .helpers.constants import LLMISVC_APP_NAME as LLMISVC_APP
from .helpers.constants import LLMISVC_GPU_MODEL_NAME
from .helpers.deploy import deploy_serving_stack
from .helpers.images import STORAGE_INITIALIZER_IMAGE, VLLM_IMAGE
from .helpers.llm_integrator_ops import (
    deploy_llm_integrator,
    relate_llm_integrator,
    remove_llm_integrator,
    wait_llm_integrator_active,
    wait_llm_integrator_blocked,
)
from .helpers.llmisvc_ops import apply_llmisvc_example, delete_llmisvc_example
from .helpers.s3_integrator import deploy_s3_integrator

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
LWS_APP = LWS_CONTROLLER.charm
# The llm-integrator charm renders a single LLMInferenceService from config. It
# supports hf:// (public or gated) and s3:// (credentials supplied via an
# s3-integrator relation) model URIs.
#
# The hf:// test exercises a gated model (google/gemma-3-270m-it), pulled with a
# Hugging Face token supplied through a Juju user secret. The token is read from
# the environment (local export or CI secret).
HF_TOKEN = os.environ.get("HF_TOKEN", "")
HF_MODEL_URI = "hf://google/gemma-3-270m-it"
HF_MODEL_NAME = "google/gemma-3-270m-it"
# Juju user-secret label holding the HF token handed to llm-integrator. Distinct
# from the K8s Secret the charm renders for the workload (named
# ``{app}-hf-token``, asserted absent after removal).
HF_TOKEN_JUJU_SECRET_LABEL = "hf-token"
LLM_INTEGRATOR_HF_SECRET = f"{LLM_INTEGRATOR_APP}-hf-token"
# The s3:// test uses the small public pythia model staged in the S3 test bucket.
LLM_INTEGRATOR_MODEL_NAME = "EleutherAI/pythia-70m"
# The no-GPU test never starts a workload, so the public hf:// copy is enough.
PUBLIC_HF_MODEL_URI = f"hf://{LLM_INTEGRATOR_MODEL_NAME}"
# s3-integrator supplies the bucket credentials for an s3:// model URI.
S3_INTEGRATOR_APP = S3_INTEGRATOR.charm
# Name of the K8s Secret the llm-integrator charm creates for s3:// models
# (``{app.name}-s3-creds``); asserted absent after the charm is removed.
LLM_INTEGRATOR_S3_SECRET = f"{LLM_INTEGRATOR_APP}-s3-creds"
TEST_DATA_DIR = Path(__file__).parent / "test_data"
# The test model lives in a Canonical S3 bucket (avoids the flaky HF CDN). The
# AWS credentials are supplied via the environment (local export or CI secrets).
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
MODEL_S3_URI = os.environ.get("TEST_MODEL_S3_URI", "s3://charmed-kubeflow-llm-storage/pythia-70m")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")


# Fail fast (rather than skip) if the S3 credentials for the test model are missing.
# GPU runs only deploy a public hf:// model, so they need no credentials.
@pytest.fixture(scope="session", autouse=True)
def require_aws_credentials(request: pytest.FixtureRequest):
    if request.config.getoption("--run-gpu-tests"):
        return
    if not (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY):
        pytest.fail(
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set to fetch the test "
            "model from S3; export them locally or provide them via CI secrets.",
            pytrace=False,
        )


# Fail fast (rather than skip) if the HF token for the gated model is missing.
@pytest.fixture(scope="session", autouse=True)
def require_hf_token(request: pytest.FixtureRequest):
    if request.config.getoption("--run-gpu-tests"):
        return
    if not HF_TOKEN:
        pytest.fail(
            "HF_TOKEN must be set to fetch the gated model via hf://; export it "
            "locally or provide it via CI secrets.",
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
# Standalone LLMInferenceService (no llm-integrator) in prefill/decode mode, a superset of
# single-worker serving; single-worker is covered through the charm tests.
PREFILL_DECODE_EXAMPLE_NAME = "test-llm-prefill-decode"
PREFILL_DECODE_EXAMPLE = TEST_DATA_DIR / "llmisvc_test_llm_prefill_decode.yaml.j2"


@pytest.mark.abort_on_fail
def test_setup_charms(juju: jubilant.Juju, charms_path: str):
    if not PREFILL_DECODE_EXAMPLE.exists():
        raise RuntimeError(f"LLMInferenceService manifest not found: {PREFILL_DECODE_EXAMPLE!s}")

    logger.info("Starting bundle integration test setup")
    deploy_serving_stack(juju, charms_path)
    logger.info("Charm setup complete")


@pytest.mark.abort_on_fail
@pytest.mark.cpu_only
def test_run_prefill_decode_example(juju: jubilant.Juju):
    logger.info("Applying a standalone prefill/decode LLMInferenceService")
    apply_llmisvc_example(
        manifest_path=str(PREFILL_DECODE_EXAMPLE),
        context=LLMISVC_IMAGE_CONTEXT,
        name=PREFILL_DECODE_EXAMPLE_NAME,
    )

    assert_llmisvc_serving(gateway_namespace=juju.model, name=PREFILL_DECODE_EXAMPLE_NAME)
    assert_llmisvc_metrics_endpoints(namespace=juju.model)

    logger.info("Deleting the example after validation")
    delete_llmisvc_example(name=PREFILL_DECODE_EXAMPLE_NAME)


@pytest.mark.abort_on_fail
@pytest.mark.cpu_only
def test_deploy_llm_via_charm(juju: jubilant.Juju, charms_path: str):
    logger.info("Providing the Hugging Face token via a Juju secret")
    secret_uri = juju.cli(
        "add-secret",
        HF_TOKEN_JUJU_SECRET_LABEL,
        f"token={HF_TOKEN}",
    ).strip()

    logger.info("Deploying llm-integrator with a gated hf:// model, requesting too much memory")
    deploy_llm_integrator(
        juju,
        charms_path,
        {
            "model-uri": HF_MODEL_URI,
            "model-name": HF_MODEL_NAME,
            "hf-token-secret": secret_uri,
            "memory-request": "10Ti",
            "memory-limit": "10Ti",
        },
    )

    logger.info("Granting the HF token secret to llm-integrator")
    juju.cli("grant-secret", HF_TOKEN_JUJU_SECRET_LABEL, LLM_INTEGRATOR_APP)

    logger.info("Waiting for llm-integrator to block on missing kserve-llmisvc relation")
    wait_llm_integrator_blocked(juju, "kserve-llmisvc")

    logger.info("Relating llm-integrator to kserve-llmisvc; it blocks as no node fits the worker")
    relate_llm_integrator(juju)
    wait_llm_integrator_blocked(juju, "No node can fit a worker")

    logger.info("Resetting the memory options; llm-integrator should recover and serve")
    juju.config(LLM_INTEGRATOR_APP, {"memory-request": None, "memory-limit": None})
    wait_llm_integrator_active(juju)

    logger.info("Verifying the charm-created LLMInferenceService serves completions")
    assert_llmisvc_serving(
        gateway_namespace=juju.model,
        name=LLM_INTEGRATOR_APP,
        model=HF_MODEL_NAME,
        namespace=juju.model,
    )

    remove_llm_integrator(juju, secret_name=LLM_INTEGRATOR_HF_SECRET)


@pytest.mark.abort_on_fail
@pytest.mark.cpu_only
def test_deploy_llm_via_charm_s3(juju: jubilant.Juju, charms_path: str):
    logger.info("Deploying s3-integrator and providing S3 credentials via a Juju secret")
    deploy_s3_integrator(
        juju,
        model_s3_uri=MODEL_S3_URI,
        endpoint=f"https://{LLMISVC_IMAGE_CONTEXT['s3_endpoint']}",
        region=AWS_REGION,
        access_key=AWS_ACCESS_KEY_ID,
        secret_key=AWS_SECRET_ACCESS_KEY,
    )
    juju.wait(lambda status: status.apps[S3_INTEGRATOR_APP].is_active, timeout=600)

    logger.info("Deploying llm-integrator with an s3:// model URI in prefill/decode mode")
    deploy_llm_integrator(
        juju,
        charms_path,
        {
            "model-uri": MODEL_S3_URI,
            "model-name": LLM_INTEGRATOR_MODEL_NAME,
            "enable-prefill-decode": True,
        },
    )
    wait_llm_integrator_blocked(juju, "kserve-llmisvc")

    logger.info("Relating llm-integrator to kserve-llmisvc; it stays blocked without S3 creds")
    relate_llm_integrator(juju)
    wait_llm_integrator_blocked(juju, "s3-integrator")

    logger.info("Relating llm-integrator to s3-integrator")
    juju.integrate(f"{LLM_INTEGRATOR_APP}:s3-credentials", f"{S3_INTEGRATOR_APP}:s3-credentials")
    wait_llm_integrator_active(juju)

    logger.info("Verifying the s3-backed LLMInferenceService serves completions")
    assert_llmisvc_serving(
        gateway_namespace=juju.model,
        name=LLM_INTEGRATOR_APP,
        model=LLM_INTEGRATOR_MODEL_NAME,
        namespace=juju.model,
    )

    remove_llm_integrator(juju, secret_name=LLM_INTEGRATOR_S3_SECRET)
    juju.remove_application(S3_INTEGRATOR_APP)
    juju.wait(lambda status: S3_INTEGRATOR_APP not in status.apps, successes=1)


@pytest.mark.abort_on_fail
@pytest.mark.cpu_only
def test_llm_integrator_blocks_without_gpus(juju: jubilant.Juju, charms_path: str):
    logger.info("Deploying llm-integrator for a GPU workload on a cluster without GPUs")
    deploy_llm_integrator(
        juju, charms_path, {"model-uri": PUBLIC_HF_MODEL_URI, "accelerator": "nvidia-gpu"}
    )
    relate_llm_integrator(juju)
    wait_llm_integrator_blocked(juju, "nvidia.com/gpu")

    remove_llm_integrator(juju)


@pytest.mark.abort_on_fail
@pytest.mark.gpu
def test_deploy_llm_via_charm_gpu(juju: jubilant.Juju, charms_path: str):
    logger.info("Deploying llm-integrator for a single-GPU workload with the default GPU image")
    deploy_llm_integrator(
        juju,
        charms_path,
        {"model-uri": f"hf://{LLMISVC_GPU_MODEL_NAME}", "accelerator": "nvidia-gpu"},
    )
    relate_llm_integrator(juju)
    wait_llm_integrator_active(juju)

    logger.info("Verifying the GPU LLMInferenceService serves completions on one GPU")
    assert_llmisvc_serving(
        gateway_namespace=juju.model,
        name=LLM_INTEGRATOR_APP,
        model=LLMISVC_GPU_MODEL_NAME,
        namespace=juju.model,
    )
    assert_workload_requests_gpus(name=LLM_INTEGRATOR_APP, namespace=juju.model)
    assert_llmisvc_metrics_endpoints(namespace=juju.model)

    remove_llm_integrator(juju)


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
    assert_no_charm_resources_left(juju.model)

    logger.info("Bundle cleanup test passed: no charm-owned resources left")
