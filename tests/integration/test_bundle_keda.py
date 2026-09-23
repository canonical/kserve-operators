#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Bundle integration test for the kserve serving stack scaled by the charmed KEDA.

Deploys the serving stack plus the ``keda`` charm and a standalone Prometheus,
applies an ``LLMInferenceService`` with no static ``replicas`` (so KEDA owns the
replica count), and drives its workload Deployment up with a Prometheus
``ScaledObject`` that scales on a live vLLM metric through the charmed KEDA
operator/metrics-apiserver. Finally it removes everything and asserts the KEDA
CRDs and charm-owned resources are cleaned up.
"""

import json
import logging
import os
from pathlib import Path

import jubilant
import pytest

from .helpers.assertions import assert_no_charm_resources_left
from .helpers.charm_paths import resolve_charm_path, resolve_charm_resources
from .helpers.charms_dependencies import (
    ENVOY_AI_CONTROLLER,
    ENVOY_CONTROLLER,
    ENVOY_INGRESS,
    SELF_SIGNED_CERTIFICATES,
)
from .helpers.constants import CONTROLLER_APP_NAME as CONTROLLER_APP
from .helpers.constants import LLMISVC_APP_NAME as LLMISVC_APP
from .helpers.constants import LWS_APP_NAME as LWS_APP
from .helpers.constants import (
    NAMESPACE_DEFAULT,
)
from .helpers.deploy import deploy_serving_stack
from .helpers.keda_ops import (
    apply_prometheus_scaledobject,
    assert_crd_absent,
    assert_deployment_replicas,
    assert_deployment_scaled_to,
    assert_external_metrics_apiservice_available,
    delete_scaledobject,
    sustained_workload_load,
)
from .helpers.llmisvc_ops import apply_llmisvc_example, delete_llmisvc_example
from .helpers.retry import RETRY_FOR_THREE_MINUTES

logger = logging.getLogger(__name__)
logging.getLogger("jubilant.wait").setLevel("WARNING")

KEDA_APP = "keda-controller"
# Standalone Prometheus (lighter than full cos-lite) to scrape the llmisvc vLLM
# metrics via the metrics-endpoint relation and back a metric-driven ScaledObject.
PROMETHEUS_CHARM = "prometheus-k8s"
PROMETHEUS_APP = "prometheus"
PROMETHEUS_CHANNEL = "1/stable"
LLM_MODEL_NAME = "EleutherAI/pythia-70m"
KEDA_SCALEDOBJECTS_CRD = "scaledobjects.keda.sh"

LLM_NAME = "keda-bundle-llm"
# The single-node workload Deployment KServe generates for the LLMInferenceService.
LLM_DEPLOYMENT = f"{LLM_NAME}-kserve"

TEST_DATA_DIR = Path(__file__).parent / "test_data"
LLM_MANIFEST = TEST_DATA_DIR / "llmisvc_keda_scale.yaml.j2"

REPO_ROOT = Path(__file__).parent.parent.parent
VLLM_IMAGE = json.loads(
    (REPO_ROOT / "charms/kserve-llmisvc/src/default-custom-images.json").read_text()
)["vllm"]
STORAGE_INITIALIZER_IMAGE = json.loads(
    (REPO_ROOT / "charms/kserve-controller/src/default-custom-images.json").read_text()
)["configmap__storageInitializer"]

# The test model is pulled from a Canonical S3 bucket (avoids the flaky HF CDN),
# matching the main bundle test. Credentials come from the environment.
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
MODEL_S3_URI = os.environ.get("TEST_MODEL_S3_URI", "s3://charmed-kubeflow-llm-storage/pythia-70m")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

LLM_CONTEXT = {
    "name": LLM_NAME,
    "vllm_image": VLLM_IMAGE,
    "storage_initializer_image": STORAGE_INITIALIZER_IMAGE,
    "model_s3_uri": MODEL_S3_URI,
    "aws_access_key_id": AWS_ACCESS_KEY_ID,
    "aws_secret_access_key": AWS_SECRET_ACCESS_KEY,
    "aws_region": AWS_REGION,
    "s3_endpoint": os.environ.get("S3_ENDPOINT", f"s3.{AWS_REGION}.amazonaws.com"),
}


# Fail fast if the S3 credentials for the test model are missing.
@pytest.fixture(scope="session", autouse=True)
def require_aws_credentials():
    if not (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY):
        pytest.fail(
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set to fetch the test "
            "model from S3; export them locally or provide them via CI secrets.",
            pytrace=False,
        )


ENVOY_APPS = (
    ENVOY_CONTROLLER.charm,
    ENVOY_AI_CONTROLLER.charm,
    ENVOY_INGRESS.charm,
    SELF_SIGNED_CERTIFICATES.charm,
)


@pytest.mark.abort_on_fail
def test_setup_stack_and_keda(juju: jubilant.Juju, request: pytest.FixtureRequest):
    charms_path = request.config.getoption("--charms-path")
    if not charms_path:
        raise ValueError("--charms-path is required for the keda bundle integration test")
    if not LLM_MANIFEST.exists():
        raise RuntimeError(f"LLMInferenceService manifest not found: {LLM_MANIFEST!s}")

    deploy_serving_stack(juju, charms_path)

    logger.info("Deploying the keda charm")
    keda_charm = resolve_charm_path(charms_path=charms_path, charm_name=KEDA_APP)
    keda_resources = resolve_charm_resources(charm_name=KEDA_APP)
    juju.deploy(charm=str(keda_charm), resources=keda_resources, trust=True)

    logger.info("Waiting for all charms (stack + keda) to be active")
    juju.wait(jubilant.all_active)

    logger.info("Verifying KEDA external-metrics APIService is Available")
    assert_external_metrics_apiservice_available()


def _integrate(juju: jubilant.Juju, provider: str, requirer: str) -> None:
    """Integrate two endpoints, treating an already-present relation as success."""
    try:
        juju.integrate(provider, requirer)
    except jubilant.CLIError as exc:
        if "already exists" not in str(exc):
            raise


@pytest.mark.abort_on_fail
def test_keda_scales_llmisvc_on_prometheus_metric(juju: jubilant.Juju):
    logger.info("Deploying standalone Prometheus and relating it to kserve-llmisvc")
    if PROMETHEUS_APP not in juju.status().apps:
        # Charmhub resolution can flake transiently; retry the deploy.
        for attempt in RETRY_FOR_THREE_MINUTES:
            with attempt:
                try:
                    juju.deploy(
                        PROMETHEUS_CHARM,
                        app=PROMETHEUS_APP,
                        channel=PROMETHEUS_CHANNEL,
                        trust=True,
                    )
                except jubilant.CLIError as exc:
                    if "already exists" in str(exc):
                        break
                    raise
    juju.wait(lambda status: PROMETHEUS_APP in status.apps, successes=1)
    _integrate(juju, f"{LLMISVC_APP}:metrics-endpoint", f"{PROMETHEUS_APP}:metrics-endpoint")
    juju.wait(jubilant.all_active)

    logger.info("Applying LLMInferenceService '%s' (no static replicas)", LLM_NAME)
    apply_llmisvc_example(
        manifest_path=str(LLM_MANIFEST),
        context=LLM_CONTEXT,
        name=LLM_NAME,
    )
    assert_deployment_replicas(LLM_DEPLOYMENT, NAMESPACE_DEFAULT, 1)

    # KEDA queries the in-cluster Prometheus directly; standalone prometheus-k8s
    # serves the API at the service root (no Traefik route-prefix).
    server_address = f"http://{PROMETHEUS_APP}.{juju.model}.svc.cluster.local:9090"
    query = (
        f'sum(vllm:num_requests_running{{k8s_namespace="{NAMESPACE_DEFAULT}",'
        f'k8s_pod_name=~"{LLM_DEPLOYMENT}-.*"}})'
    )
    logger.info("Applying a Prometheus ScaledObject targeting %s", LLM_DEPLOYMENT)
    apply_prometheus_scaledobject(
        name=LLM_NAME,
        target_deployment=LLM_DEPLOYMENT,
        server_address=server_address,
        query=query,
        threshold="1",
        max_replicas=2,
    )

    logger.info("Driving sustained load; KEDA should scale the workload up to 2 replicas")
    with sustained_workload_load(LLM_NAME, LLM_MODEL_NAME, NAMESPACE_DEFAULT):
        # Assert on KEDA's desired replica count (the HPA reacting to the metric),
        # not ready replicas: a second 3Gi vLLM pod may not schedule on a
        # resource-constrained CI runner, which is not what this test verifies.
        assert_deployment_scaled_to(LLM_DEPLOYMENT, NAMESPACE_DEFAULT, 2)

    logger.info("Cleaning up the ScaledObject and LLMInferenceService")
    delete_scaledobject(LLM_NAME)
    delete_llmisvc_example(name=LLM_NAME)


def test_remove_leaves_no_charm_resources(juju: jubilant.Juju):
    # Tear down in dependency order. kserve-llmisvc's remove hook deletes the
    # serving CRDs and waits for them to terminate; a residual CR only unblocks
    # once kserve-controller clears its finalizer, so kserve-llmisvc must be
    # fully removed while the controller is still up. Under CI's
    # automatically-retry-hooks=false a single stuck remove hook never recovers.
    logger.info("Removing keda and prometheus first")
    for app in (KEDA_APP, PROMETHEUS_APP):
        if app in juju.status().apps:
            juju.remove_application(app)
    juju.wait(
        lambda status: KEDA_APP not in status.apps and PROMETHEUS_APP not in status.apps,
        successes=1,
    )

    logger.info("Removing kserve-llmisvc while kserve-controller is still present")
    if LLMISVC_APP in juju.status().apps:
        juju.remove_application(LLMISVC_APP)
    juju.wait(lambda status: LLMISVC_APP not in status.apps, successes=1)

    logger.info("Removing kserve-controller, lws-controller and the envoy stack")
    for app in (CONTROLLER_APP, LWS_APP, *ENVOY_APPS):
        if app in juju.status().apps:
            juju.remove_application(app)
    juju.wait(
        lambda status: CONTROLLER_APP not in status.apps and LWS_APP not in status.apps,
        successes=1,
    )

    logger.info("Verifying charm-owned resources and KEDA CRDs are gone")
    assert_no_charm_resources_left(juju.model)
    assert_crd_absent(KEDA_SCALEDOBJECTS_CRD)
