#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Deploy, wait and cleanup helpers for the llm-integrator charm."""

import logging
from typing import Optional

import jubilant

from .assertions import assert_llminferenceservice_absent, assert_secret_absent
from .charm_paths import resolve_charm_path
from .constants import LLM_INTEGRATOR_APP_NAME, LLMISVC_APP_NAME

logger = logging.getLogger(__name__)


def deploy_llm_integrator(juju: jubilant.Juju, charms_path: str, config: dict) -> None:
    """Deploy the locally built llm-integrator charm with the given config."""
    charm = resolve_charm_path(charms_path=charms_path, charm_name=LLM_INTEGRATOR_APP_NAME)
    juju.deploy(charm=str(charm), config=config, trust=True)


def relate_llm_integrator(juju: jubilant.Juju) -> None:
    """Relate llm-integrator to kserve-llmisvc, which gates it on readiness."""
    juju.integrate(
        f"{LLM_INTEGRATOR_APP_NAME}:kserve-llmisvc", f"{LLMISVC_APP_NAME}:kserve-llmisvc"
    )


def wait_llm_integrator_blocked(juju: jubilant.Juju, message: str) -> None:
    """Wait until llm-integrator is Blocked with ``message`` in its status."""
    juju.wait(
        lambda status: status.apps[LLM_INTEGRATOR_APP_NAME].is_blocked
        and message in status.apps[LLM_INTEGRATOR_APP_NAME].app_status.message,
        successes=1,
    )


def wait_llm_integrator_active(juju: jubilant.Juju) -> None:
    """Wait until llm-integrator is Active, i.e. its LLMInferenceService is Ready."""
    juju.wait(lambda status: status.apps[LLM_INTEGRATOR_APP_NAME].is_active, successes=1)


def remove_llm_integrator(juju: jubilant.Juju, secret_name: Optional[str] = None) -> None:
    """Remove llm-integrator and verify its LLMInferenceService (and Secret) are gone."""
    logger.info("Removing %s and verifying its resources are cleaned up", LLM_INTEGRATOR_APP_NAME)
    juju.remove_application(LLM_INTEGRATOR_APP_NAME)
    juju.wait(lambda status: LLM_INTEGRATOR_APP_NAME not in status.apps, successes=1)
    assert_llminferenceservice_absent(name=LLM_INTEGRATOR_APP_NAME, namespace=juju.model)
    if secret_name:
        assert_secret_absent(name=secret_name, namespace=juju.model)
