# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Polling and assertion helpers for the keda charm integration tests."""

import logging

import lightkube
import tenacity
from lightkube.core.exceptions import ApiError
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.resources.apiregistration_v1 import APIService
from lightkube.resources.apps_v1 import Deployment
from lightkube.resources.autoscaling_v2 import HorizontalPodAutoscaler
from lightkube.resources.batch_v1 import Job

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
def wait_for_hpa_exists(client: lightkube.Client, name: str, namespace: str) -> None:
    """Block until the named HorizontalPodAutoscaler exists (raises until found)."""
    client.get(HorizontalPodAutoscaler, name=name, namespace=namespace)


def hpa_exists(client: lightkube.Client, name: str, namespace: str) -> bool:
    """Return whether the named HorizontalPodAutoscaler currently exists."""
    try:
        client.get(HorizontalPodAutoscaler, name=name, namespace=namespace)
    except ApiError as exc:
        if exc.status.code == 404:
            return False
        raise
    return True


@RETRY
def wait_for_jobs(
    client: lightkube.Client, namespace: str, labels: dict, min_count: int = 1
) -> None:
    """Block until at least ``min_count`` Jobs matching the label selector exist."""
    jobs = list(client.list(Job, namespace=namespace, labels=labels))
    assert len(jobs) >= min_count, f"expected >= {min_count} jobs, found {len(jobs)}"


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
