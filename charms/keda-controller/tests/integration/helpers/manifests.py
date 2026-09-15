# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Kubernetes manifest builders for the keda charm integration tests.

The KEDA custom resources (``ScaledObject``/``ScaledJob``) are exposed here as
lightkube generic resources, alongside factory functions that build the
throwaway workloads and scalers the tests apply.
"""

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from lightkube.generic_resource import create_namespaced_resource
from lightkube.models.apps_v1 import DeploymentSpec
from lightkube.models.core_v1 import (
    Container,
    ContainerPort,
    PodSpec,
    PodTemplateSpec,
    ServicePort,
    ServiceSpec,
)
from lightkube.models.meta_v1 import LabelSelector, ObjectMeta
from lightkube.resources.apps_v1 import Deployment
from lightkube.resources.core_v1 import Service

from tests.integration.helpers.constants import (
    METRIC_SRC_IMAGE,
    METRIC_SRC_NAME,
    METRIC_SRC_PORT,
    METRICS_API_DEPLOYMENT,
    METRICS_API_MAX_REPLICAS,
    METRICS_API_SCALEDOBJECT,
    METRICS_API_TARGET_VALUE,
    SMOKE_IMAGE,
)

ScaledObject = create_namespaced_resource("keda.sh", "v1alpha1", "ScaledObject", "scaledobjects")
ScaledJob = create_namespaced_resource("keda.sh", "v1alpha1", "ScaledJob", "scaledjobs")


def _active_cron_trigger(desired_replicas: int) -> dict:
    """A cron trigger whose active window is anchored around the current time.

    A fixed 00:00-23:59 window has a one-minute inactive gap at midnight UTC
    (KEDA deactivates at the end event until the next start), which any test
    crossing that minute could hit. Anchoring the window to "now" keeps that gap
    at least an hour away from a minutes-long test.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=1)
    end = now + timedelta(hours=6)
    return {
        "type": "cron",
        "metadata": {
            "timezone": "Etc/UTC",
            "start": f"{start.minute} {start.hour} * * *",
            "end": f"{end.minute} {end.hour} * * *",
            "desiredReplicas": str(desired_replicas),
        },
    }


def pause_deployment(name: str, namespace: str, replicas: int = 1) -> Deployment:
    """A minimal always-running Deployment used as a scale target."""
    return Deployment(
        metadata=ObjectMeta(name=name, namespace=namespace),
        spec=DeploymentSpec(
            replicas=replicas,
            selector=LabelSelector(matchLabels={"app": name}),
            template=PodTemplateSpec(
                metadata=ObjectMeta(labels={"app": name}),
                spec=PodSpec(containers=[Container(name="pause", image=SMOKE_IMAGE)]),
            ),
        ),
    )


def cron_scaledobject(name: str, target: str, namespace: str, max_replicas: int):
    """A ScaledObject whose cron trigger is active all day (always desires max)."""
    return ScaledObject(
        metadata=ObjectMeta(name=name, namespace=namespace),
        spec={
            "scaleTargetRef": {"name": target},
            "minReplicaCount": 1,
            "maxReplicaCount": max_replicas,
            "pollingInterval": 5,
            "triggers": [_active_cron_trigger(max_replicas)],
        },
    )


def cron_scaledjob(name: str, namespace: str, max_replicas: int):
    """A ScaledJob whose cron trigger spawns Jobs up to ``max_replicas``."""
    return ScaledJob(
        metadata=ObjectMeta(name=name, namespace=namespace),
        spec={
            "jobTargetRef": {
                "template": {
                    "spec": {
                        "containers": [{"name": "worker", "image": SMOKE_IMAGE}],
                        "restartPolicy": "Never",
                    }
                }
            },
            "maxReplicaCount": max_replicas,
            "pollingInterval": 5,
            "triggers": [_active_cron_trigger(max_replicas)],
        },
    )


def metric_source_deployment(namespace: str) -> Deployment:
    """An agnhost echo server that reflects the JSON metric back to KEDA."""
    return Deployment(
        metadata=ObjectMeta(name=METRIC_SRC_NAME, namespace=namespace),
        spec=DeploymentSpec(
            replicas=1,
            selector=LabelSelector(matchLabels={"app": METRIC_SRC_NAME}),
            template=PodTemplateSpec(
                metadata=ObjectMeta(labels={"app": METRIC_SRC_NAME}),
                spec=PodSpec(
                    containers=[
                        Container(
                            name="agnhost",
                            image=METRIC_SRC_IMAGE,
                            args=["netexec", f"--http-port={METRIC_SRC_PORT}"],
                            ports=[ContainerPort(containerPort=METRIC_SRC_PORT)],
                        )
                    ]
                ),
            ),
        ),
    )


def metric_source_service(namespace: str) -> Service:
    """The Service fronting the agnhost metric source."""
    return Service(
        metadata=ObjectMeta(name=METRIC_SRC_NAME, namespace=namespace),
        spec=ServiceSpec(
            selector={"app": METRIC_SRC_NAME},
            ports=[ServicePort(port=METRIC_SRC_PORT, targetPort=METRIC_SRC_PORT)],
        ),
    )


def metrics_api_scaledobject(namespace: str, metric_value: int):
    """A metrics-api ScaledObject reading ``value`` from the agnhost echo endpoint.

    The metric is embedded in the echo URL, so re-applying with a new value
    retunes the scaler. Scale-down/-to-zero timers are shortened so the tests do
    not wait on KEDA's and the HPA's default stabilisation windows.
    """
    echo_body = quote(json.dumps({"value": metric_value}))
    url = (
        f"http://{METRIC_SRC_NAME}.{namespace}.svc.cluster.local:{METRIC_SRC_PORT}"
        f"/echo?msg={echo_body}"
    )
    return ScaledObject(
        metadata=ObjectMeta(name=METRICS_API_SCALEDOBJECT, namespace=namespace),
        spec={
            "scaleTargetRef": {"name": METRICS_API_DEPLOYMENT},
            "minReplicaCount": 0,
            "maxReplicaCount": METRICS_API_MAX_REPLICAS,
            "pollingInterval": 5,
            "cooldownPeriod": 5,
            "advanced": {
                "horizontalPodAutoscalerConfig": {
                    "behavior": {
                        "scaleDown": {
                            "stabilizationWindowSeconds": 0,
                            "policies": [{"type": "Percent", "value": 100, "periodSeconds": 15}],
                        }
                    }
                }
            },
            "triggers": [
                {
                    "type": "metrics-api",
                    "metadata": {
                        "url": url,
                        "valueLocation": "value",
                        "targetValue": METRICS_API_TARGET_VALUE,
                    },
                }
            ],
        },
    )
