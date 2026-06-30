# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

import lightkube
import tenacity
from lightkube import ApiError
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.resources.apps_v1 import StatefulSet

logger = logging.getLogger(__name__)


@tenacity.retry(
    wait=tenacity.wait_fixed(5),
    stop=tenacity.stop_after_delay(60 * 5),
    reraise=True,
)
def wait_for_crd_established(client: lightkube.Client, crd_name: str) -> None:
    """Wait until the given CRD exists and reports the ``Established`` condition."""
    crd = client.get(CustomResourceDefinition, name=crd_name)
    conditions = (crd.status.conditions if crd.status else None) or []
    established = any(
        condition.type == "Established" and condition.status == "True" for condition in conditions
    )
    if not established:
        raise AssertionError(f"CRD '{crd_name}' is not Established yet: {conditions}")
    logger.info("CRD '%s' is Established", crd_name)


@tenacity.retry(
    wait=tenacity.wait_fixed(10),
    stop=tenacity.stop_after_delay(60 * 10),
    reraise=True,
)
def wait_for_leader_statefulset_ready(client: lightkube.Client, name: str, namespace: str) -> None:
    """Wait until the leader StatefulSet created for ``name`` has all replicas ready.

    The LWS controller creates a leader StatefulSet named after the
    LeaderWorkerSet resource. Each ready leader replica in turn manages its own
    worker StatefulSet, so a ready leader StatefulSet indicates the controller
    successfully reconciled the workload.
    """
    statefulset = client.get(StatefulSet, name=name, namespace=namespace)
    spec_replicas = statefulset.spec.replicas if statefulset.spec else None
    ready_replicas = statefulset.status.readyReplicas if statefulset.status else None
    if ready_replicas != spec_replicas or spec_replicas is None:
        raise AssertionError(
            f"Leader StatefulSet '{name}' not ready: "
            f"readyReplicas={ready_replicas}, replicas={spec_replicas}"
        )
    logger.info(
        "Leader StatefulSet '%s' has %s/%s ready replicas", name, ready_replicas, spec_replicas
    )


@tenacity.retry(
    wait=tenacity.wait_fixed(5),
    stop=tenacity.stop_after_delay(60 * 5),
    reraise=True,
)
def wait_for_resource_deleted(
    client: lightkube.Client, resource, name: str, namespace=None
) -> None:
    """Wait until the given cluster or namespaced resource no longer exists."""
    try:
        client.get(resource, name=name, namespace=namespace)
    except ApiError as error:
        if error.status.code == 404:
            logger.info("Resource '%s' is gone", name)
            return
        raise
    raise AssertionError(f"Resource '{name}' still exists")
