#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Apply/delete lifecycle helpers for LLMInferenceService examples."""

import logging

from jinja2 import Template
from lightkube.core.exceptions import ApiError

from .constants import LLMISVC_NAME, NAMESPACE_DEFAULT
from .k8s import LLMInferenceService, apply_yaml, get_client
from .retry import (
    RETRY_FOR_TEN_MINUTES,
    RETRY_FOR_THREE_MINUTES,
    RETRY_FOR_TWENTY_MINUTES,
)

logger = logging.getLogger(__name__)


def _render_manifest(manifest_path: str, context: dict) -> str:
    """Render a Jinja2 LLMInferenceService manifest template with the given context."""
    with open(manifest_path, "r") as template_file:
        return Template(template_file.read()).render(context)


def apply_llmisvc_example(manifest_path: str, context: dict, name: str = LLMISVC_NAME) -> None:
    logger.info("Applying LLMInferenceService example from %s...", manifest_path)
    rendered_manifest = _render_manifest(manifest_path, context)
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            apply_yaml(rendered_manifest)

    logger.info(
        "Waiting for LLMInferenceService routing and workloads readiness (up to 20 minutes)..."
    )
    client = get_client()
    for attempt in RETRY_FOR_TWENTY_MINUTES:
        with attempt:
            llmisvc = client.get(LLMInferenceService, name=name, namespace=NAMESPACE_DEFAULT)
            status = llmisvc.status or {}
            conditions = {
                condition.get("type"): condition.get("status")
                for condition in status.get("conditions", [])
            }

            routes_ready = conditions.get("HTTPRoutesReady") == "True"
            workloads_ready = conditions.get("WorkloadsReady") == "True"

            if routes_ready and workloads_ready:
                logger.info(
                    "LLMInferenceService has HTTP routes and workloads ready; "
                    "proceeding to route checks"
                )
                return

            raise AssertionError(
                "LLMInferenceService not ready enough yet: "
                f"HTTPRoutesReady={conditions.get('HTTPRoutesReady')}, "
                f"WorkloadsReady={conditions.get('WorkloadsReady')}, "
                f"Ready={conditions.get('Ready')}"
            )


def delete_llmisvc_example(name: str = LLMISVC_NAME) -> None:
    """Delete the LLMInferenceService example and wait for it to be fully gone.

    This must run while the kserve controller is still deployed. The
    LLMInferenceService carries a controller-managed finalizer
    (``serving.kserve.io/llmisvc-finalizer``); if the charm (and therefore the
    controller) is removed first, nothing clears the finalizer, the custom
    resource is stuck, and the ``llminferenceservices.serving.kserve.io`` CRD
    can never finish terminating -- leaving charm-owned resources behind.
    """
    logger.info("Deleting LLMInferenceService '%s'...", name)
    client = get_client()
    try:
        client.delete(LLMInferenceService, name=name, namespace=NAMESPACE_DEFAULT)
    except ApiError as err:
        if err.status.code != 404:
            raise

    logger.info("Waiting for LLMInferenceService '%s' to be fully removed...", name)
    for attempt in RETRY_FOR_TEN_MINUTES:
        with attempt:
            try:
                client.get(LLMInferenceService, name=name, namespace=NAMESPACE_DEFAULT)
            except ApiError as err:
                if err.status.code == 404:
                    break
                raise
            raise AssertionError(
                f"LLMInferenceService '{name}' still present (finalizer not cleared yet)"
            )
    logger.info("LLMInferenceService '%s' fully removed", name)
