#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.


import logging
import urllib.request
from pathlib import Path

import pytest
import yaml
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest):
    """Build the charm-under-test and deploy it together with related charms.

    Assert on the unit status before any relations/configurations take place.
    """
    # build and deploy charm from local source folder
    charm = await ops_test.build_charm(".")
    resources = {
        "kserve-web-app-image": METADATA["resources"]["kserve-web-app-image"]["upstream-source"]
    }
    await ops_test.model.deploy(charm, resources=resources, application_name=APP_NAME)

    # issuing dummy update_status just to trigger an event
    async with ops_test.fast_forward():
        await ops_test.model.wait_for_idle(
            apps=[APP_NAME],
            status="active",
            raise_on_blocked=True,
            timeout=1000,
        )
        assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"


async def test_relations(ops_test: OpsTest):
    istio_pilot = "istio-pilot"
    istio_gateway = "istio-ingressgateway"

    await ops_test.model.deploy(
        entity_url=istio_pilot,
        channel="latest/edge",
        config={"default-gateway": "kubeflow-gateway"},
        trust=True,
    )

    await ops_test.model.deploy(
        entity_url="istio-gateway",
        application_name=istio_gateway,
        channel="latest/edge",
        config={"kind": "ingress"},
        trust=True,
    )
    await ops_test.model.add_relation(
        istio_pilot,
        istio_gateway,
    )

    await ops_test.model.wait_for_idle(
        [istio_pilot, istio_gateway],
        raise_on_blocked=False,
        status="active",
        timeout=90 * 10,
    )

    await ops_test.model.add_relation(f"{istio_pilot}:ingress", f"{APP_NAME}:ingress")

    await ops_test.model.wait_for_idle(
        status="active",
        raise_on_blocked=False,
        raise_on_error=True,
        timeout=600,
    )
