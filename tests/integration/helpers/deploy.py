#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Deploy the KServe serving stack (mirrors the bundle test setup).

Extracted so tests other than the bundle (e.g. the observability suite) can stand
up the same stack without duplicating the deploy/relate sequence.
"""

import logging

import jubilant

from .charm_paths import resolve_charm_path, resolve_charm_resources
from .charms_dependencies import (
    ENVOY_AI_CONTROLLER,
    ENVOY_CONTROLLER,
    ENVOY_INGRESS,
    SELF_SIGNED_CERTIFICATES,
)

logger = logging.getLogger(__name__)

CONTROLLER_APP = "kserve-controller"
LLMISVC_APP = "kserve-llmisvc"
LWS_APP = "lws-controller"


def deploy_serving_stack(juju: jubilant.Juju, charms_path: str) -> None:
    """Deploy and relate the Envoy gateway + KServe serving charms, waiting for active."""
    controller_charm = resolve_charm_path(charms_path=charms_path, charm_name=CONTROLLER_APP)
    llmisvc_charm = resolve_charm_path(charms_path=charms_path, charm_name=LLMISVC_APP)
    lws_charm = resolve_charm_path(charms_path=charms_path, charm_name=LWS_APP)
    controller_resources = resolve_charm_resources(charm_name=CONTROLLER_APP)
    llmisvc_resources = resolve_charm_resources(charm_name=LLMISVC_APP)
    lws_resources = resolve_charm_resources(charm_name=LWS_APP)

    logger.info("Deploying Envoy gateway charm stack")
    for dep in (ENVOY_CONTROLLER, ENVOY_AI_CONTROLLER, ENVOY_INGRESS, SELF_SIGNED_CERTIFICATES):
        juju.deploy(dep.charm, channel=dep.channel, trust=dep.trust, config=dep.config)

    logger.info("Relating Envoy charms")
    juju.integrate(ENVOY_AI_CONTROLLER.charm, SELF_SIGNED_CERTIFICATES.charm)
    juju.integrate(ENVOY_CONTROLLER.charm, ENVOY_AI_CONTROLLER.charm)

    logger.info("Deploying lws-controller charm")
    juju.deploy(charm=str(lws_charm), resources=lws_resources, trust=True)

    logger.info("Deploying kserve-controller charm")
    juju.deploy(
        charm=str(controller_charm),
        resources=controller_resources,
        config={"deployment-mode": "standard"},
        trust=True,
    )

    logger.info("Waiting for kserve-controller to block on missing gateway-metadata relation")
    juju.wait(lambda status: CONTROLLER_APP in status.apps, successes=1)
    juju.wait(lambda status: status.apps[CONTROLLER_APP].is_blocked, successes=1)

    logger.info("Relating kserve-controller to the Envoy gateway metadata provider")
    juju.integrate(f"{CONTROLLER_APP}:gateway-metadata", f"{ENVOY_INGRESS.charm}:gateway-metadata")

    logger.info("Deploying kserve-llmisvc charm")
    juju.deploy(charm=str(llmisvc_charm), resources=llmisvc_resources, trust=True)
    juju.wait(lambda status: LLMISVC_APP in status.apps, successes=1)

    logger.info("Relating kserve charms")
    juju.integrate("kserve-controller:kserve-controller", "kserve-llmisvc:kserve-controller")
    juju.integrate("lws-controller:lws-controller", "kserve-llmisvc:lws-controller")

    logger.info("Waiting for all charms to be active")
    juju.wait(jubilant.all_active, successes=1)
    logger.info("Serving stack ready")
