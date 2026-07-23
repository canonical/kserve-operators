#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the lws-controller charm.

The suite is intentionally narrow: it verifies that the charm deploys and
becomes active, that the LeaderWorkerSet CRD it ships is installed and
``Established``, that the controller actually reconciles a sample
LeaderWorkerSet into a running leader StatefulSet, and that removing the
application cleans up the cluster-scoped resources (CRD and webhook
configurations) it created.
"""

import logging
from pathlib import Path

import jubilant
import lightkube
import pytest
import yaml
from lightkube.resources.admissionregistration_v1 import (
    MutatingWebhookConfiguration,
    ValidatingWebhookConfiguration,
)
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition

from tests.integration.constants import (
    APP_NAME,
    IMAGE_RESOURCES,
    LWS_CRD_NAME,
    LWS_SAMPLE_CR_NAME,
    LWS_SAMPLE_CR_PATH,
    MUTATING_WEBHOOK_NAME,
    VALIDATING_WEBHOOK_NAME,
    LeaderWorkerSet,
)
from tests.integration.utils import (
    wait_for_crd_established,
    wait_for_leader_statefulset_ready,
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

    logger.info("Deploying %s from %s", APP_NAME, charm)
    juju.deploy(
        charm=str(charm),
        app=APP_NAME,
        resources=IMAGE_RESOURCES,
        trust=True,
    )

    logger.info("Waiting for %s to be active", APP_NAME)
    juju.wait(jubilant.all_active)


def test_lws_crd_is_established(juju: jubilant.Juju, lightkube_client: lightkube.Client):
    """The charm should install the LeaderWorkerSet CRD and make it Established."""
    wait_for_crd_established(lightkube_client, LWS_CRD_NAME)


def test_leaderworkerset_is_reconciled(juju: jubilant.Juju, lightkube_client: lightkube.Client):
    """Apply a sample LeaderWorkerSet and confirm the controller reconciles it."""
    namespace = juju.model
    lws_manifest = yaml.safe_load(LWS_SAMPLE_CR_PATH.read_text())
    lws = LeaderWorkerSet.from_dict(lws_manifest)
    lws.metadata.namespace = namespace

    logger.info(
        "Applying sample LeaderWorkerSet '%s' in namespace '%s'", LWS_SAMPLE_CR_NAME, namespace
    )
    lightkube_client.apply(lws, namespace=namespace)

    try:
        # The LWS controller creates a leader StatefulSet named after the CR.
        wait_for_leader_statefulset_ready(
            lightkube_client, name=LWS_SAMPLE_CR_NAME, namespace=namespace
        )
    finally:
        logger.info("Deleting sample LeaderWorkerSet '%s'", LWS_SAMPLE_CR_NAME)
        lightkube_client.delete(LeaderWorkerSet, name=LWS_SAMPLE_CR_NAME, namespace=namespace)


VALID_MANAGER_CONFIG = """apiVersion: config.lws.x-k8s.io/v1alpha1
kind: Configuration
leaderElection:
  leaderElect: true
internalCertManagement:
  enable: false
clientConnection:
  qps: 400
  burst: 400
"""

# Missing the required ``internalCertManagement.enable: false`` key.
INVALID_MANAGER_CONFIG = """apiVersion: config.lws.x-k8s.io/v1alpha1
kind: Configuration
leaderElection:
  leaderElect: true
"""


def test_invalid_manager_config_blocks(juju: jubilant.Juju):
    """An invalid manager-config should block the unit, then recover on reset."""
    try:
        logger.info("Setting an invalid manager-config")
        juju.config(APP_NAME, {"manager-config": INVALID_MANAGER_CONFIG})
        juju.wait(lambda status: status.apps[APP_NAME].is_blocked)
    finally:
        logger.info("Resetting manager-config back to default")
        juju.config(APP_NAME, {"manager-config": ""})
        juju.wait(jubilant.all_active)


def test_valid_manager_config_override(juju: jubilant.Juju):
    """A valid manager-config override should keep the unit active, then reset."""
    try:
        logger.info("Setting a valid manager-config override")
        juju.config(APP_NAME, {"manager-config": VALID_MANAGER_CONFIG})
        juju.wait(jubilant.all_active)
    finally:
        logger.info("Resetting manager-config back to default")
        juju.config(APP_NAME, {"manager-config": ""})
        juju.wait(jubilant.all_active)


def test_charm_removal_cleans_up(juju: jubilant.Juju, lightkube_client: lightkube.Client):
    """Removing the application must delete the CRD and webhook configurations."""
    logger.info("Removing application %s", APP_NAME)
    juju.remove_application(APP_NAME, destroy_storage=True)

    logger.info("Waiting for %s to disappear from the model", APP_NAME)
    juju.wait(lambda status: APP_NAME not in status.apps)

    logger.info("Verifying cluster-scoped resources were cleaned up")
    wait_for_resource_deleted(lightkube_client, CustomResourceDefinition, name=LWS_CRD_NAME)
    wait_for_resource_deleted(
        lightkube_client, MutatingWebhookConfiguration, name=MUTATING_WEBHOOK_NAME
    )
    wait_for_resource_deleted(
        lightkube_client, ValidatingWebhookConfiguration, name=VALIDATING_WEBHOOK_NAME
    )
