#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the keda charm.

Covers the full surface of the charmed KEDA:

- deploys and becomes active;
- installs the KEDA CRDs and registers the ``external.metrics.k8s.io`` APIService
  (the aggregation-layer TLS through the charmed metrics-apiserver);
- ScaledObjects/ScaledJobs drive workload replica counts (and the managed HPA);
- the admission webhook rejects invalid resources;
- the ``watch-namespace`` config scopes reconciliation;
- removing the application cleans up the cluster-scoped resources it created.
"""

import logging
import time
from pathlib import Path

import jubilant
import lightkube
import pytest
from lightkube.core.exceptions import ApiError
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.resources.apiregistration_v1 import APIService
from lightkube.resources.apps_v1 import Deployment
from lightkube.resources.batch_v1 import Job
from lightkube.resources.core_v1 import Namespace, Service

from tests.integration.helpers.constants import (
    APP_NAME,
    EXTERNAL_METRICS_APISERVICE,
    HPA_NAME_PREFIX,
    IMAGE_RESOURCES,
    KEDA_CRDS,
    METRIC_SRC_NAME,
    METRICS_API_DEPLOYMENT,
    METRICS_API_MAX_REPLICAS,
    METRICS_API_SCALEDOBJECT,
    SCALEDJOB_LABEL,
    SCALEDJOB_MAX_REPLICAS,
    SCALEDJOB_NAME,
    SECOND_WATCHED_NAMESPACE,
    SMOKE_DEPLOYMENT,
    SMOKE_MAX_REPLICAS,
    SMOKE_SCALEDOBJECT,
    UNWATCHED_NAMESPACE,
    UNWATCHED_SCALEDOBJECT,
    UNWATCHED_STABILITY_CHECKS,
    UNWATCHED_STABILITY_INTERVAL_SECONDS,
    UNWATCHED_WORKLOAD,
    WATCH_NS_MAX_REPLICAS,
    WATCHED_SCALEDOBJECT,
    WATCHED_WORKLOAD,
    WEBHOOK_DEPLOYMENT,
    WEBHOOK_SCALEDOBJECT_A,
    WEBHOOK_SCALEDOBJECT_B,
)
from tests.integration.helpers.manifests import (
    ScaledJob,
    ScaledObject,
    cron_scaledjob,
    cron_scaledobject,
    metric_source_deployment,
    metric_source_service,
    metrics_api_scaledobject,
    pause_deployment,
)
from tests.integration.helpers.waiters import (
    hpa_exists,
    wait_for_apiservice_available,
    wait_for_crd_established,
    wait_for_deployment_replicas,
    wait_for_hpa_exists,
    wait_for_jobs,
    wait_for_resource_deleted,
)

logger = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
def test_build_and_deploy(juju: jubilant.Juju, request: pytest.FixtureRequest):
    """Deploy the locally-built charm and wait for it to become active."""
    charm_path = request.config.getoption("--charm-path")
    if not charm_path:
        raise ValueError("--charm-path is required for the integration tests")

    charm = Path(charm_path).absolute()
    if not charm.exists():
        raise FileNotFoundError(f"Charm file not found: {charm!s}")

    # Idempotent for --keep-models reruns: only deploy if not already present.
    if APP_NAME not in juju.status().apps:
        logger.info("Deploying %s from %s", APP_NAME, charm)
        juju.deploy(charm=str(charm), app=APP_NAME, resources=IMAGE_RESOURCES, trust=True)

    logger.info("Waiting for %s to be active", APP_NAME)
    juju.wait(jubilant.all_active)


def test_crds_established(lightkube_client: lightkube.Client):
    """All KEDA CRDs the charm ships should be installed and Established."""
    for crd in KEDA_CRDS:
        wait_for_crd_established(lightkube_client, crd)


def test_external_metrics_apiservice_available(lightkube_client: lightkube.Client):
    """The external-metrics APIService must be Available (aggregation TLS works)."""
    wait_for_apiservice_available(lightkube_client, EXTERNAL_METRICS_APISERVICE)


def test_scaledobject_drives_replicas(juju: jubilant.Juju, lightkube_client: lightkube.Client):
    """A cron ScaledObject should scale a throwaway Deployment up to its max replicas."""
    namespace = juju.model

    deployment = pause_deployment(SMOKE_DEPLOYMENT, namespace)
    scaledobject = cron_scaledobject(
        SMOKE_SCALEDOBJECT, SMOKE_DEPLOYMENT, namespace, SMOKE_MAX_REPLICAS
    )

    try:
        lightkube_client.apply(deployment, namespace=namespace)
        lightkube_client.apply(scaledobject, namespace=namespace)
        wait_for_deployment_replicas(
            lightkube_client, SMOKE_DEPLOYMENT, namespace, SMOKE_MAX_REPLICAS
        )
        # KEDA reconciles a managed HPA (keda-hpa-<name>) behind every ScaledObject.
        wait_for_hpa_exists(lightkube_client, f"{HPA_NAME_PREFIX}{SMOKE_SCALEDOBJECT}", namespace)
    finally:
        lightkube_client.delete(ScaledObject, name=SMOKE_SCALEDOBJECT, namespace=namespace)
        lightkube_client.delete(Deployment, name=SMOKE_DEPLOYMENT, namespace=namespace)


def test_admission_webhook_rejects_duplicate_scaledobject(
    juju: jubilant.Juju, lightkube_client: lightkube.Client
):
    """The validating webhook must reject a second ScaledObject on the same target."""
    namespace = juju.model

    deployment = pause_deployment(WEBHOOK_DEPLOYMENT, namespace)
    scaledobject_a = cron_scaledobject(WEBHOOK_SCALEDOBJECT_A, WEBHOOK_DEPLOYMENT, namespace, 2)
    scaledobject_b = cron_scaledobject(WEBHOOK_SCALEDOBJECT_B, WEBHOOK_DEPLOYMENT, namespace, 2)

    try:
        lightkube_client.apply(deployment, namespace=namespace)
        lightkube_client.apply(scaledobject_a, namespace=namespace)

        # A second ScaledObject targeting the same workload is denied by the webhook.
        with pytest.raises(ApiError) as excinfo:
            lightkube_client.create(scaledobject_b, namespace=namespace)
        assert excinfo.value.status.code in (400, 403), (
            f"expected an admission denial, got HTTP {excinfo.value.status.code}: "
            f"{excinfo.value.status.message}"
        )
    finally:
        for name in (WEBHOOK_SCALEDOBJECT_B, WEBHOOK_SCALEDOBJECT_A):
            try:
                lightkube_client.delete(ScaledObject, name=name, namespace=namespace)
            except ApiError as exc:
                if exc.status.code != 404:
                    raise
        lightkube_client.delete(Deployment, name=WEBHOOK_DEPLOYMENT, namespace=namespace)


def test_scaledjob_creates_jobs(juju: jubilant.Juju, lightkube_client: lightkube.Client):
    """A cron ScaledJob should spawn Jobs owned by KEDA up to its max replicas."""
    namespace = juju.model

    scaledjob = cron_scaledjob(SCALEDJOB_NAME, namespace, SCALEDJOB_MAX_REPLICAS)

    try:
        lightkube_client.apply(scaledjob, namespace=namespace)
        wait_for_jobs(lightkube_client, namespace, {SCALEDJOB_LABEL: SCALEDJOB_NAME})
    finally:
        # Deleting the ScaledJob cascades to its owned Jobs; sweep any stragglers,
        # tolerating races where the garbage collector already removed them.
        lightkube_client.delete(ScaledJob, name=SCALEDJOB_NAME, namespace=namespace)
        for job in lightkube_client.list(
            Job, namespace=namespace, labels={SCALEDJOB_LABEL: SCALEDJOB_NAME}
        ):
            try:
                lightkube_client.delete(
                    Job, name=job.metadata.name, namespace=namespace, grace_period=0
                )
            except ApiError as exc:
                if exc.status.code != 404:
                    raise


def test_metrics_api_scaler_up_down_zero(juju: jubilant.Juju, lightkube_client: lightkube.Client):
    """A metrics-api ScaledObject drives a workload up, back down, and to zero.

    This exercises the metrics-apiserver end to end (external.metrics.k8s.io) and
    KEDA's activation path, proving metric-driven scaling in both directions.
    """
    namespace = juju.model

    metric_src = metric_source_deployment(namespace)
    metric_svc = metric_source_service(namespace)
    workload = pause_deployment(METRICS_API_DEPLOYMENT, namespace)

    try:
        lightkube_client.apply(metric_src, namespace=namespace)
        lightkube_client.apply(metric_svc, namespace=namespace)
        lightkube_client.apply(workload, namespace=namespace)
        wait_for_deployment_replicas(lightkube_client, METRIC_SRC_NAME, namespace, 1)

        # metric well above target -> scale up to max.
        lightkube_client.apply(metrics_api_scaledobject(namespace, 100), namespace=namespace)
        wait_for_deployment_replicas(
            lightkube_client, METRICS_API_DEPLOYMENT, namespace, METRICS_API_MAX_REPLICAS
        )

        # lower the metric -> scale down: 20 / METRICS_API_TARGET_VALUE (10) = 2 replicas.
        lightkube_client.apply(metrics_api_scaledobject(namespace, 20), namespace=namespace)
        wait_for_deployment_replicas(lightkube_client, METRICS_API_DEPLOYMENT, namespace, 2)

        # metric goes inactive (0) -> KEDA scales the workload to zero.
        lightkube_client.apply(metrics_api_scaledobject(namespace, 0), namespace=namespace)
        wait_for_deployment_replicas(lightkube_client, METRICS_API_DEPLOYMENT, namespace, 0)
    finally:
        lightkube_client.delete(ScaledObject, name=METRICS_API_SCALEDOBJECT, namespace=namespace)
        lightkube_client.delete(Deployment, name=METRICS_API_DEPLOYMENT, namespace=namespace)
        lightkube_client.delete(Service, name=METRIC_SRC_NAME, namespace=namespace)
        lightkube_client.delete(Deployment, name=METRIC_SRC_NAME, namespace=namespace)


def test_watch_namespace_scopes_reconciliation(
    juju: jubilant.Juju, lightkube_client: lightkube.Client
):
    """A comma-separated watch-namespace confines KEDA to exactly those namespaces.

    ScaledObjects in the two watched namespaces both scale; an identical one in a
    third, unwatched namespace is ignored (no managed HPA, no scaling).
    """
    first_ns = juju.model

    # Create the extra namespaces before scoping KEDA to the two watched ones.
    for ns in (SECOND_WATCHED_NAMESPACE, UNWATCHED_NAMESPACE):
        lightkube_client.apply(Namespace(metadata=ObjectMeta(name=ns)))

    # Restrict KEDA to two namespaces; the operator restarts with the new env.
    juju.config(APP_NAME, {"watch-namespace": f"{first_ns},{SECOND_WATCHED_NAMESPACE}"})
    juju.wait(jubilant.all_active)

    first_wl = pause_deployment(WATCHED_WORKLOAD, first_ns)
    first_so = cron_scaledobject(
        WATCHED_SCALEDOBJECT, WATCHED_WORKLOAD, first_ns, WATCH_NS_MAX_REPLICAS
    )
    second_wl = pause_deployment(WATCHED_WORKLOAD, SECOND_WATCHED_NAMESPACE)
    second_so = cron_scaledobject(
        WATCHED_SCALEDOBJECT, WATCHED_WORKLOAD, SECOND_WATCHED_NAMESPACE, WATCH_NS_MAX_REPLICAS
    )
    unwatched_wl = pause_deployment(UNWATCHED_WORKLOAD, UNWATCHED_NAMESPACE)
    unwatched_so = cron_scaledobject(
        UNWATCHED_SCALEDOBJECT, UNWATCHED_WORKLOAD, UNWATCHED_NAMESPACE, WATCH_NS_MAX_REPLICAS
    )

    try:
        for wl, ns in (
            (first_wl, first_ns),
            (second_wl, SECOND_WATCHED_NAMESPACE),
            (unwatched_wl, UNWATCHED_NAMESPACE),
        ):
            lightkube_client.apply(wl, namespace=ns)
        for so, ns in (
            (first_so, first_ns),
            (second_so, SECOND_WATCHED_NAMESPACE),
            (unwatched_so, UNWATCHED_NAMESPACE),
        ):
            lightkube_client.apply(so, namespace=ns)

        # Both watched namespaces scale, proving the comma-separated list is honoured.
        wait_for_deployment_replicas(
            lightkube_client, WATCHED_WORKLOAD, first_ns, WATCH_NS_MAX_REPLICAS
        )
        wait_for_deployment_replicas(
            lightkube_client, WATCHED_WORKLOAD, SECOND_WATCHED_NAMESPACE, WATCH_NS_MAX_REPLICAS
        )

        # The third, unwatched namespace stays ignored: no HPA and no scaling.
        unwatched_hpa = f"{HPA_NAME_PREFIX}{UNWATCHED_SCALEDOBJECT}"
        for _ in range(UNWATCHED_STABILITY_CHECKS):
            assert not hpa_exists(
                lightkube_client, unwatched_hpa, UNWATCHED_NAMESPACE
            ), "KEDA created an HPA for a ScaledObject outside its watch namespaces"
            deployment = lightkube_client.get(
                Deployment, name=UNWATCHED_WORKLOAD, namespace=UNWATCHED_NAMESPACE
            )
            ready = (deployment.status.readyReplicas or 0) if deployment.status else 0
            assert ready <= 1, f"unwatched workload scaled to {ready} replicas"
            time.sleep(UNWATCHED_STABILITY_INTERVAL_SECONDS)
    finally:
        # first_ns is the model namespace and cannot be deleted; clean its resources.
        lightkube_client.delete(ScaledObject, name=WATCHED_SCALEDOBJECT, namespace=first_ns)
        lightkube_client.delete(Deployment, name=WATCHED_WORKLOAD, namespace=first_ns)
        # Deleting the extra namespaces cascades to their workloads and ScaledObjects.
        for ns in (SECOND_WATCHED_NAMESPACE, UNWATCHED_NAMESPACE):
            lightkube_client.delete(Namespace, name=ns)
        juju.config(APP_NAME, {"watch-namespace": ""})
        juju.wait(jubilant.all_active)


def test_remove_cleans_up_cluster_resources(
    juju: jubilant.Juju, lightkube_client: lightkube.Client
):
    """Removing the application deletes the CRDs and the external-metrics APIService."""
    logger.info("Removing %s", APP_NAME)
    juju.remove_application(APP_NAME, destroy_storage=True)
    juju.wait(lambda status: APP_NAME not in status.apps)

    wait_for_resource_deleted(lightkube_client, APIService, EXTERNAL_METRICS_APISERVICE)
    for crd in KEDA_CRDS:
        wait_for_resource_deleted(lightkube_client, CustomResourceDefinition, crd)
