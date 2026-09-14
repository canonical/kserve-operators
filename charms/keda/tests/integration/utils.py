# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Polling helpers for the keda charm integration tests."""

import logging

import lightkube
import tenacity
from lightkube.core.exceptions import ApiError
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.resources.apiregistration_v1 import APIService
from lightkube.resources.apps_v1 import Deployment

logger = logging.getLogger(__name__)

RETRY = tenacity.retry(
    stop=tenacity.stop_after_delay(300),
    wait=tenacity.wait_fixed(5),
    reraise=True,
)


def _condition_true(conditions, cond_type) -> bool:
    for condition in conditions or []:
        # lightkube returns typed condition objects (attributes), not dicts.
        if getattr(condition, "type", None) == cond_type and (
            getattr(condition, "status", None) == "True"
        ):
            return True
    return False


@RETRY
def wait_for_crd_established(client: lightkube.Client, name: str) -> None:
    """Block until the named CRD reports the Established condition."""
    crd = client.get(CustomResourceDefinition, name=name)
    conditions = crd.status.conditions if crd.status else []
    assert _condition_true(conditions, "Established"), f"CRD {name} not Established yet"


@RETRY
def wait_for_apiservice_available(client: lightkube.Client, name: str) -> None:
    """Block until the named APIService reports Available=True."""
    api = client.get(APIService, name=name)
    conditions = api.status.conditions if api.status else []
    assert _condition_true(conditions, "Available"), f"APIService {name} not Available yet"


@RETRY
def wait_for_deployment_replicas(
    client: lightkube.Client, name: str, namespace: str, replicas: int
) -> None:
    """Block until a Deployment reports the expected number of ready replicas."""
    deployment = client.get(Deployment, name=name, namespace=namespace)
    ready = (deployment.status.readyReplicas or 0) if deployment.status else 0
    assert ready == replicas, f"Deployment {name} has {ready}/{replicas} ready replicas"


@RETRY
def wait_for_resource_deleted(client: lightkube.Client, resource, name: str) -> None:
    """Block until a cluster-scoped resource no longer exists."""
    try:
        client.get(resource, name=name)
    except ApiError as exc:
        if exc.status.code == 404:
            return
        raise
    raise AssertionError(f"{resource.__name__} {name} still exists")
