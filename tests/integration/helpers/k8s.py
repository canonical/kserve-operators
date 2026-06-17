#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Lightkube client and resource helpers shared by the bundle integration tests.

A single cached lightkube client is used across the setup and assertion helpers.
The custom resources the bundle interacts with (Gateway API, the inference
extension and KServe's ``LLMInferenceService``) are exposed here as lightkube
generic resources so the rest of the suite can ``get``/``list``/``apply`` them
with the typed client instead of shelling out to ``kubectl``.
"""

import functools
import logging

import lightkube
import lightkube.codecs
from lightkube.generic_resource import (
    create_global_resource,
    create_namespaced_resource,
)
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.resources.apps_v1 import Deployment

from .retry import RETRY_FOR_THREE_MINUTES

logger = logging.getLogger(__name__)

FIELD_MANAGER = "kserve-bundle-tests"

# Gateway API (gateway.networking.k8s.io/v1) resources the bundle creates and inspects.
GatewayClass = create_global_resource(
    "gateway.networking.k8s.io", "v1", "GatewayClass", "gatewayclasses"
)
Gateway = create_namespaced_resource("gateway.networking.k8s.io", "v1", "Gateway", "gateways")
HTTPRoute = create_namespaced_resource("gateway.networking.k8s.io", "v1", "HTTPRoute", "httproutes")

# KServe LLMInferenceService custom resource.
LLMInferenceService = create_namespaced_resource(
    "serving.kserve.io", "v1alpha2", "LLMInferenceService", "llminferenceservices"
)


@functools.lru_cache(maxsize=1)
def get_client() -> lightkube.Client:
    """Return a process-wide cached lightkube client for the bundle tests."""
    return lightkube.Client(field_manager=FIELD_MANAGER)


def generic_resource_for_crd(crd_name: str):
    """Build a lightkube generic resource class from an installed CRD.

    The served version and scope are read from the cluster so callers don't have
    to hard-code an API version that may change between upstream releases.
    """
    crd = get_client().get(CustomResourceDefinition, crd_name)
    group = crd.spec.group
    kind = crd.spec.names.kind
    plural = crd.spec.names.plural
    version = next(v.name for v in crd.spec.versions if v.served)
    if crd.spec.scope == "Namespaced":
        return create_namespaced_resource(group, version, kind, plural)
    return create_global_resource(group, version, kind, plural)


def apply_yaml(manifest: str) -> None:
    """Server-side apply every document in a (multi-doc) YAML manifest string."""
    client = get_client()
    for obj in lightkube.codecs.load_all_yaml(manifest):
        client.apply(obj)


def _conditions(status) -> list:
    """Return the ``conditions`` list from a typed or generic resource status."""
    if status is None:
        return []
    if isinstance(status, dict):
        return status.get("conditions", []) or []
    return getattr(status, "conditions", []) or []


def _condition_value(condition, key: str):
    """Read a field from a condition entry, tolerating dict or model form."""
    if isinstance(condition, dict):
        return condition.get(key)
    return getattr(condition, key, None)


def wait_for_crd_established(crd_name: str) -> None:
    """Block until the named CRD reports the ``Established`` condition."""
    client = get_client()
    logger.info("Waiting for CRD '%s' to be established...", crd_name)
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            crd = client.get(CustomResourceDefinition, crd_name)
            established = any(
                _condition_value(c, "type") == "Established"
                and _condition_value(c, "status") == "True"
                for c in _conditions(crd.status)
            )
            if not established:
                raise AssertionError(f"CRD '{crd_name}' is not Established yet")
    logger.info("CRD '%s' is established", crd_name)


def wait_for_deployment_available(namespace: str, name: str) -> None:
    """Block until the named Deployment has finished rolling out."""
    client = get_client()
    logger.info("Waiting for deployment '%s/%s' to be available...", namespace, name)
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            deployment = client.get(Deployment, name=name, namespace=namespace)
            desired = (deployment.spec.replicas if deployment.spec else None) or 0
            status = deployment.status
            available = (getattr(status, "availableReplicas", None) or 0) if status else 0
            updated = (getattr(status, "updatedReplicas", None) or 0) if status else 0
            if desired == 0 or available < desired or updated < desired:
                raise AssertionError(
                    f"Deployment '{namespace}/{name}' not rolled out yet: "
                    f"desired={desired}, available={available}, updated={updated}"
                )
    logger.info("Deployment '%s/%s' is available", namespace, name)
