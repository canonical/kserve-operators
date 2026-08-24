#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Observability integration test for kserve-llmisvc.

Self-contained (own model): deploys the serving stack, applies a CPU
LLMInferenceService, relates kserve-llmisvc directly to cos-lite (prometheus,
grafana, loki), generates a little inference traffic, and asserts that the
charm's metrics, alert rules, Grafana dashboards and logs reached COS.

Heavy (stands up cos-lite) — run via its own ``cos-integration`` tox env.
"""

import json
import logging
import os
from pathlib import Path

import jubilant
import pytest

from .helpers.constants import LLMISVC_APP_NAME, LLMISVC_MODEL_NAME, LLMISVC_NAME, NAMESPACE_DEFAULT
from .helpers.cos import (
    alert_present,
    dashboard_present,
    generate_inference_traffic,
    get_cos_address,
    metric_has_samples,
    published_grafana_dashboards,
    published_loki_logs,
    published_prometheus_alerts,
    query_prometheus,
)
from .helpers.deploy import deploy_serving_stack
from .helpers.llmisvc_ops import apply_llmisvc_example
from .helpers.retry import RETRY_FOR_TEN_MINUTES, RETRY_FOR_THREE_MINUTES

logger = logging.getLogger(__name__)
logging.getLogger("jubilant.wait").setLevel("WARNING")

pytestmark = pytest.mark.cos

COS_LITE = "cos-lite"
COS_LITE_CHANNEL = "latest/stable"
PROMETHEUS_APP = "prometheus"
GRAFANA_APP = "grafana"
LOKI_APP = "loki"
COS_APPS = ("prometheus", "grafana", "loki", "alertmanager", "traefik", "catalogue")

TEST_DATA_DIR = Path(__file__).parent / "test_data"
CPU_EXAMPLE = TEST_DATA_DIR / "llmisvc_test_llm_scheduler_small.yaml.j2"

REPO_ROOT = Path(__file__).parent.parent.parent
KSERVE_CONTROLLER_IMAGES = json.loads(
    (REPO_ROOT / "charms/kserve-controller/src/default-custom-images.json").read_text()
)
KSERVE_LLMISVC_IMAGES = json.loads(
    (REPO_ROOT / "charms/kserve-llmisvc/src/default-custom-images.json").read_text()
)
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
MODEL_S3_URI = os.environ.get("TEST_MODEL_S3_URI", "s3://charmed-kubeflow-llm-storage/pythia-70m")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

IMAGE_CONTEXT = {
    "storage_initializer_image": KSERVE_CONTROLLER_IMAGES["configmap__storageInitializer"],
    "vllm_image": KSERVE_LLMISVC_IMAGES["vllm"],
    "model_s3_uri": MODEL_S3_URI,
    "aws_access_key_id": AWS_ACCESS_KEY_ID,
    "aws_secret_access_key": AWS_SECRET_ACCESS_KEY,
    "aws_region": AWS_REGION,
    "s3_endpoint": os.environ.get("S3_ENDPOINT", f"s3.{AWS_REGION}.amazonaws.com"),
}

# Signals we expect to reach COS. Metric names must match the charm's alert rules
# (src/prometheus_alert_rules/) and dashboards (src/grafana_dashboards/).
CONTROLLER_METRIC = f'up{{juju_application="{LLMISVC_APP_NAME}"}}'
VLLM_METRIC_CANDIDATES = (
    "vllm:num_requests_running",
    "vllm:kv_cache_usage_perc",
    "vllm:gpu_cache_usage_perc",
)
EXPECTED_ALERTS = ("KServeLLMISVCTargetDown", "KServeLLMISVCKVCacheSaturated")
EXPECTED_DASHBOARDS = ("KServe LLMISVC - Controller", "KServe LLMISVC - vLLM Workloads")


@pytest.fixture(scope="module", autouse=True)
def require_aws_credentials():
    if not (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY):
        pytest.fail(
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set to fetch the test model.",
            pytrace=False,
        )


def _integrate(juju: jubilant.Juju, provider: str, requirer: str) -> None:
    """Integrate two endpoints, treating an already-present relation as success."""
    try:
        juju.integrate(provider, requirer)
    except jubilant.CLIError as exc:
        if "already exists" not in str(exc):
            raise


@pytest.mark.abort_on_fail
def test_deploy_stack_and_model(juju: jubilant.Juju, request: pytest.FixtureRequest):
    charms_path = request.config.getoption("--charms-path")
    if not charms_path:
        raise ValueError("--charms-path is required for the observability integration test")
    if not CPU_EXAMPLE.exists():
        raise RuntimeError(f"LLMInferenceService manifest not found: {CPU_EXAMPLE!s}")

    deploy_serving_stack(juju, charms_path)

    logger.info("Applying CPU LLMInferenceService example")
    apply_llmisvc_example(manifest_path=str(CPU_EXAMPLE), context=IMAGE_CONTEXT, name=LLMISVC_NAME)


@pytest.mark.abort_on_fail
def test_relate_to_cos(juju: jubilant.Juju):
    # Idempotent + retried: cos-lite resolution over Charmhub can flake transiently,
    # and re-running against a kept model must not fail on already-present apps/relations.
    if not all(app in juju.status().apps for app in COS_APPS):
        logger.info("Deploying cos-lite")
        for attempt in RETRY_FOR_THREE_MINUTES:
            with attempt:
                try:
                    juju.deploy(COS_LITE, channel=COS_LITE_CHANNEL, trust=True)
                except jubilant.CLIError as exc:
                    if "already exists" in str(exc):
                        break
                    raise
    juju.wait(lambda status: all(app in status.apps for app in COS_APPS), successes=1)

    logger.info("Relating kserve-llmisvc directly to COS (metrics, dashboards, logging)")
    _integrate(juju, f"{LLMISVC_APP_NAME}:metrics-endpoint", f"{PROMETHEUS_APP}:metrics-endpoint")
    _integrate(juju, f"{LLMISVC_APP_NAME}:grafana-dashboard", f"{GRAFANA_APP}:grafana-dashboard")
    _integrate(juju, f"{LLMISVC_APP_NAME}:logging", f"{LOKI_APP}:logging")

    logger.info("Waiting for all charms to be active after COS relations")
    juju.wait(jubilant.all_active, successes=1)


@pytest.mark.abort_on_fail
def test_generate_traffic():
    generate_inference_traffic(
        isvc_name=LLMISVC_NAME, model_name=LLMISVC_MODEL_NAME, namespace=NAMESPACE_DEFAULT
    )


def test_cos_data_published(juju: jubilant.Juju):
    """Assert metrics, alert rules, dashboards and logs all reached COS."""
    for attempt in RETRY_FOR_TEN_MINUTES:
        with attempt:
            host = get_cos_address(juju)

            logger.info("Checking controller metrics reached Prometheus...")
            assert metric_has_samples(query_prometheus(juju, host, CONTROLLER_METRIC))

            logger.info("Checking vLLM workload metrics reached Prometheus...")
            assert any(
                metric_has_samples(query_prometheus(juju, host, metric))
                for metric in VLLM_METRIC_CANDIDATES
            ), "no vLLM (vllm:*) metrics found in Prometheus"

            logger.info("Checking alert rules were published...")
            alerts = published_prometheus_alerts(juju, host)
            for alert in EXPECTED_ALERTS:
                assert alert_present(alerts, alert), f"alert rule '{alert}' not published"

            logger.info("Checking Grafana dashboards were published...")
            dashboards = published_grafana_dashboards(juju)
            for title in EXPECTED_DASHBOARDS:
                assert dashboard_present(dashboards, title), f"dashboard '{title}' not published"

            logger.info("Checking charm logs reached Loki...")
            logs = published_loki_logs(juju, "juju_application", LLMISVC_APP_NAME)
            assert logs and logs.get("data", {}).get("result"), "no kserve-llmisvc logs in Loki"
