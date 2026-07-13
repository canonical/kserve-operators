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

logger = logging.getLogger(__name__)

FIELD_MANAGER = "kserve-bundle-tests"

# Gateway API (gateway.networking.k8s.io/v1) resources the bundle inspects.
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
