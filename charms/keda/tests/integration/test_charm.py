#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the keda charm.

Verifies that the charm deploys and becomes active, installs the KEDA CRDs and
registers the ``external.metrics.k8s.io`` APIService (i.e. the aggregation-layer
TLS works through the charmed metrics-apiserver), that a ScaledObject actually
drives a workload's replica count, and that removing the application cleans up
the cluster-scoped resources (CRDs and APIService) it created.
"""

import logging
from pathlib import Path

import jubilant
import lightkube
import pytest
from lightkube.models.apps_v1 import DeploymentSpec
from lightkube.models.core_v1 import Container, PodSpec, PodTemplateSpec
from lightkube.models.meta_v1 import LabelSelector, ObjectMeta
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.resources.apiregistration_v1 import APIService
from lightkube.resources.apps_v1 import Deployment

from tests.integration.constants import (
    APP_NAME,
    EXTERNAL_METRICS_APISERVICE,
    IMAGE_RESOURCES,
    KEDA_CRDS,
    SMOKE_DEPLOYMENT,
    SMOKE_IMAGE,
    SMOKE_MAX_REPLICAS,
    SMOKE_SCALEDOBJECT,
    ScaledObject,
)
from tests.integration.utils import (
    wait_for_apiservice_available,
    wait_for_crd_established,
    wait_for_deployment_replicas,
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

    deployment = Deployment(
        metadata=ObjectMeta(name=SMOKE_DEPLOYMENT, namespace=namespace),
        spec=DeploymentSpec(
            replicas=1,
            selector=LabelSelector(matchLabels={"app": SMOKE_DEPLOYMENT}),
            template=PodTemplateSpec(
                metadata=ObjectMeta(labels={"app": SMOKE_DEPLOYMENT}),
                spec=PodSpec(containers=[Container(name="pause", image=SMOKE_IMAGE)]),
            ),
        ),
    )
    # Cron window spanning the whole day so the trigger is always active.
    scaledobject = ScaledObject(
        metadata=ObjectMeta(name=SMOKE_SCALEDOBJECT, namespace=namespace),
        spec={
            "scaleTargetRef": {"name": SMOKE_DEPLOYMENT},
            "minReplicaCount": 1,
            "maxReplicaCount": SMOKE_MAX_REPLICAS,
            "pollingInterval": 5,
            "triggers": [
                {
                    "type": "cron",
                    "metadata": {
                        "timezone": "Etc/UTC",
                        "start": "0 0 * * *",
                        "end": "59 23 * * *",
                        "desiredReplicas": str(SMOKE_MAX_REPLICAS),
                    },
                }
            ],
        },
    )

    try:
        lightkube_client.apply(deployment, namespace=namespace)
        lightkube_client.apply(scaledobject, namespace=namespace)
        wait_for_deployment_replicas(
            lightkube_client, SMOKE_DEPLOYMENT, namespace, SMOKE_MAX_REPLICAS
        )
    finally:
        lightkube_client.delete(ScaledObject, name=SMOKE_SCALEDOBJECT, namespace=namespace)
        lightkube_client.delete(Deployment, name=SMOKE_DEPLOYMENT, namespace=namespace)


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
