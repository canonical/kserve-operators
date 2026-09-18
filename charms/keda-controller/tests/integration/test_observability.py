#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the keda charm's observability relations.

Deploys standalone Prometheus and Loki (no full COS stack) and verifies that:

- Prometheus scrapes all three KEDA component ``/metrics`` endpoints through the
  ``metrics-endpoint`` relation;
- keda's container logs reach Loki through the ``logging`` relation.
"""

import logging
from pathlib import Path

import jubilant
import pytest

from tests.integration.helpers.constants import (
    APP_NAME,
    EXPECTED_METRICS_PORTS,
    IMAGE_RESOURCES,
    LOKI_APP,
    LOKI_CHANNEL,
    LOKI_CHARM,
    LOKI_PORT,
    PROMETHEUS_APP,
    PROMETHEUS_CHANNEL,
    PROMETHEUS_CHARM,
    PROMETHEUS_PORT,
)
from tests.integration.helpers.observability import (
    assert_loki_has_logs,
    assert_prometheus_scrapes_ports,
)

logger = logging.getLogger(__name__)


def _integrate(juju: jubilant.Juju, provider: str, requirer: str) -> None:
    """Integrate two endpoints, treating an already-present relation as success."""
    try:
        juju.integrate(provider, requirer)
    except jubilant.CLIError as exc:
        if "already exists" not in str(exc):
            raise


@pytest.mark.abort_on_fail
def test_deploy_keda(juju: jubilant.Juju, request: pytest.FixtureRequest):
    """Deploy the locally-built keda charm and wait for it to become active."""
    charm_path = request.config.getoption("--charm-path")
    if not charm_path:
        raise ValueError("--charm-path is required for the integration tests")

    charm = Path(charm_path).absolute()
    if not charm.exists():
        raise FileNotFoundError(f"Charm file not found: {charm!s}")

    if APP_NAME not in juju.status().apps:
        logger.info("Deploying %s from %s", APP_NAME, charm)
        juju.deploy(charm=str(charm), app=APP_NAME, resources=IMAGE_RESOURCES, trust=True)

    juju.wait(jubilant.all_active)


def test_metrics_endpoint_scraped_by_prometheus(juju: jubilant.Juju):
    """Prometheus scrapes all three KEDA /metrics endpoints via metrics-endpoint."""
    if PROMETHEUS_APP not in juju.status().apps:
        logger.info("Deploying %s", PROMETHEUS_CHARM)
        juju.deploy(PROMETHEUS_CHARM, app=PROMETHEUS_APP, channel=PROMETHEUS_CHANNEL, trust=True)

    _integrate(juju, f"{APP_NAME}:metrics-endpoint", f"{PROMETHEUS_APP}:metrics-endpoint")
    juju.wait(jubilant.all_active)

    assert_prometheus_scrapes_ports(
        juju.model, PROMETHEUS_APP, PROMETHEUS_PORT, APP_NAME, EXPECTED_METRICS_PORTS
    )


def test_logging_forwarded_to_loki(juju: jubilant.Juju):
    """keda's container logs reach Loki via the logging relation."""
    if LOKI_APP not in juju.status().apps:
        logger.info("Deploying %s", LOKI_CHARM)
        juju.deploy(LOKI_CHARM, app=LOKI_APP, channel=LOKI_CHANNEL, trust=True)

    _integrate(juju, f"{APP_NAME}:logging", f"{LOKI_APP}:logging")
    juju.wait(jubilant.all_active)

    assert_loki_has_logs(juju.model, LOKI_APP, LOKI_PORT, APP_NAME)
