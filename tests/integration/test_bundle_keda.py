#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Bundle integration test for llm-integrator autoscaling with the charmed KEDA.

Deploys the serving stack, keda-controller, a standalone Prometheus scraping the
vLLM metrics and llm-integrator with autoscaling enabled. It checks that the model
serves while the autoscaling relations are missing, that the charm then creates a
ScaledObject per worker, that sustained load scales the worker up, and that
disabling autoscaling and removing the charms clean everything up. Scaled-up pods
are never waited for: the tests assert KEDA's desired replica count.
"""

import logging
import os

import jubilant
import pytest

from .helpers.assertions import assert_llmisvc_serving, assert_no_charm_resources_left
from .helpers.charm_paths import resolve_charm_path, resolve_charm_resources
from .helpers.charms_dependencies import (
    ENVOY_AI_CONTROLLER,
    ENVOY_CONTROLLER,
    ENVOY_INGRESS,
    PROMETHEUS,
    S3_INTEGRATOR,
    SELF_SIGNED_CERTIFICATES,
)
from .helpers.constants import CONTROLLER_APP_NAME as CONTROLLER_APP
from .helpers.constants import LLM_INTEGRATOR_APP_NAME as LLM_INTEGRATOR_APP
from .helpers.constants import LLMISVC_APP_NAME as LLMISVC_APP
from .helpers.constants import LWS_APP_NAME as LWS_APP
from .helpers.deploy import deploy_serving_stack
from .helpers.keda_ops import (
    assert_crd_absent,
    assert_deployment_scaled_to,
    assert_external_metrics_apiservice_available,
    assert_scaled_object_absent,
    assert_scaled_object_ready,
    sustained_workload_load,
    wait_for_scaled_object,
)
from .helpers.llm_integrator_ops import (
    deploy_llm_integrator,
    relate_llm_integrator,
    remove_llm_integrator,
    wait_llm_integrator_active,
    wait_llm_integrator_blocked,
)
from .helpers.retry import RETRY_FOR_THREE_MINUTES
from .helpers.s3_integrator import deploy_s3_integrator

logger = logging.getLogger(__name__)
logging.getLogger("jubilant.wait").setLevel("WARNING")

KEDA_APP = "keda-controller"
PROMETHEUS_APP = "prometheus"
S3_INTEGRATOR_APP = S3_INTEGRATOR.charm
KEDA_SCALEDOBJECTS_CRD = "scaledobjects.keda.sh"

# The test model is pulled from a Canonical S3 bucket (avoids the flaky HF CDN),
# matching the main bundle test. Credentials come from the environment.
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
MODEL_S3_URI = os.environ.get("TEST_MODEL_S3_URI", "s3://charmed-kubeflow-llm-storage/pythia-70m")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", f"s3.{AWS_REGION}.amazonaws.com")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
LLM_MODEL_NAME = "EleutherAI/pythia-70m"

# Worker Deployments KServe creates; the charm names each ScaledObject after its Deployment.
DECODE_DEPLOYMENT = f"{LLM_INTEGRATOR_APP}-kserve"
PREFILL_DEPLOYMENT = f"{LLM_INTEGRATOR_APP}-kserve-prefill"
MAX_REPLICAS = 2
LLM_INTEGRATOR_CONFIG = {
    "model-uri": MODEL_S3_URI,
    "model-name": LLM_MODEL_NAME,
    "max-replicas": MAX_REPLICAS,
    # One request per replica, so the load generator triggers a scale-up straight away.
    "autoscaling-target": 1.0,
    "autoscaling-polling-interval": 5,
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


def _deploy_prometheus(juju: jubilant.Juju) -> None:
    """Deploy the standalone Prometheus, retrying as Charmhub resolution can flake."""
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            try:
                juju.deploy(
                    PROMETHEUS.charm,
                    app=PROMETHEUS_APP,
                    channel=PROMETHEUS.channel,
                    trust=PROMETHEUS.trust,
                )
            except jubilant.CLIError as exc:
                if "already exists" not in str(exc):
                    raise


@pytest.mark.abort_on_fail
def test_setup(juju: jubilant.Juju, charms_path: str):
    deploy_serving_stack(juju, charms_path)

    logger.info("Deploying keda-controller, Prometheus, s3-integrator and llm-integrator")
    keda_charm = resolve_charm_path(charms_path=charms_path, charm_name=KEDA_APP)
    keda_resources = resolve_charm_resources(charm_name=KEDA_APP)
    juju.deploy(charm=str(keda_charm), resources=keda_resources, trust=True)
    _deploy_prometheus(juju)
    deploy_s3_integrator(
        juju,
        model_s3_uri=MODEL_S3_URI,
        endpoint=f"https://{S3_ENDPOINT}",
        region=AWS_REGION,
        access_key=AWS_ACCESS_KEY_ID,
        secret_key=AWS_SECRET_ACCESS_KEY,
    )
    deploy_llm_integrator(juju, charms_path, LLM_INTEGRATOR_CONFIG)

    logger.info("Relating everything except the autoscaling relations of llm-integrator")
    juju.integrate(f"{LLMISVC_APP}:metrics-endpoint", f"{PROMETHEUS_APP}:metrics-endpoint")
    juju.integrate(f"{LLM_INTEGRATOR_APP}:s3-credentials", f"{S3_INTEGRATOR_APP}:s3-credentials")
    relate_llm_integrator(juju)
    juju.wait(
        lambda status: jubilant.all_active(status, [KEDA_APP, PROMETHEUS_APP, S3_INTEGRATOR_APP])
    )
    assert_external_metrics_apiservice_available()


@pytest.mark.abort_on_fail
def test_serves_without_autoscaling_relations(juju: jubilant.Juju):
    logger.info("Waiting for llm-integrator to serve and ask for the keda relation")
    # Autoscaling problems only show in the status once the workload serves.
    wait_llm_integrator_blocked(juju, f"{KEDA_APP}:keda")
    assert_llmisvc_serving(
        gateway_namespace=juju.model,
        name=LLM_INTEGRATOR_APP,
        model=LLM_MODEL_NAME,
        namespace=juju.model,
    )


@pytest.mark.abort_on_fail
def test_charm_creates_scaled_object(juju: jubilant.Juju):
    logger.info("Relating llm-integrator to keda-controller, then to Prometheus")
    juju.integrate(f"{LLM_INTEGRATOR_APP}:keda", f"{KEDA_APP}:keda")
    wait_llm_integrator_blocked(juju, "prometheus-api")
    juju.integrate(f"{LLM_INTEGRATOR_APP}:prometheus-api", f"{PROMETHEUS_APP}:prometheus-api")
    wait_llm_integrator_active(juju)
    message = juju.status().apps[LLM_INTEGRATOR_APP].app_status.message
    assert message == f"Autoscaling 1-{MAX_REPLICAS} replicas"

    scaled_object = wait_for_scaled_object(DECODE_DEPLOYMENT, juju.model)
    assert scaled_object.spec["scaleTargetRef"] == {"name": DECODE_DEPLOYMENT}
    assert scaled_object.spec["minReplicaCount"] == 1
    assert scaled_object.spec["maxReplicaCount"] == MAX_REPLICAS
    assert scaled_object.spec["advanced"]["restoreToOriginalReplicaCount"] is True
    # Ready means KEDA's query against the related Prometheus returns the vLLM metric.
    assert_scaled_object_ready(DECODE_DEPLOYMENT, juju.model)


@pytest.mark.abort_on_fail
def test_scales_up_on_load(juju: jubilant.Juju):
    logger.info("Driving sustained load; KEDA should scale the worker to %d", MAX_REPLICAS)
    with sustained_workload_load(LLM_INTEGRATOR_APP, LLM_MODEL_NAME, juju.model):
        assert_deployment_scaled_to(DECODE_DEPLOYMENT, juju.model, MAX_REPLICAS)


def test_prefill_worker_gets_own_scaled_object(juju: jubilant.Juju):
    logger.info("Enabling prefill/decode; the prefill worker should get its own ScaledObject")
    juju.config(LLM_INTEGRATOR_APP, {"enable-prefill-decode": True})
    scaled_object = wait_for_scaled_object(PREFILL_DEPLOYMENT, juju.model)
    assert scaled_object.spec["scaleTargetRef"] == {"name": PREFILL_DEPLOYMENT}


def test_disabling_autoscaling_restores_replicas(juju: jubilant.Juju):
    logger.info("Setting max-replicas to 1; the ScaledObjects go and the worker is scaled back")
    juju.config(LLM_INTEGRATOR_APP, {"max-replicas": 1})
    for name in (DECODE_DEPLOYMENT, PREFILL_DEPLOYMENT):
        assert_scaled_object_absent(name, juju.model)
    assert_deployment_scaled_to(DECODE_DEPLOYMENT, juju.model, 1)


def test_remove_leaves_no_charm_resources(juju: jubilant.Juju):
    # Tear down in dependency order. kserve-llmisvc's remove hook deletes the
    # serving CRDs and waits for them to terminate; a residual CR only unblocks
    # once kserve-controller clears its finalizer, so kserve-llmisvc must be
    # fully removed while the controller is still up. Under CI's
    # automatically-retry-hooks=false a single stuck remove hook never recovers.
    logger.info("Removing llm-integrator and verifying its resources are gone")
    if LLM_INTEGRATOR_APP in juju.status().apps:
        remove_llm_integrator(juju, secret_name=f"{LLM_INTEGRATOR_APP}-s3-creds")

    logger.info("Removing keda, prometheus and s3-integrator")
    for app in (KEDA_APP, PROMETHEUS_APP, S3_INTEGRATOR_APP):
        if app in juju.status().apps:
            juju.remove_application(app)
    juju.wait(
        lambda status: not {KEDA_APP, PROMETHEUS_APP, S3_INTEGRATOR_APP} & set(status.apps),
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
