# Copyright 2025 Canonical Ltd.
"""Charms dependencies for tests."""

from charmed_kubeflow_chisme.testing import CharmSpec

ISTIO_GATEWAY = CharmSpec(
    charm="istio-gateway", channel="latest/edge", config={"kind": "ingress"}, trust=True
)
ISTIO_PILOT = CharmSpec(charm="istio-pilot", channel="latest/edge", trust=True)
KNATIVE_OPERATOR = CharmSpec(charm="knative-operator", channel="latest/edge", trust=True)
KNATIVE_SERVING = CharmSpec(charm="knative-serving", channel="latest/edge", trust=True)
METACONTROLLER_OPERATOR = CharmSpec(
    charm="metacontroller-operator", channel="latest/edge", trust=True
)
MINIO = CharmSpec(
    charm="minio",
    channel="latest/edge",
    trust=True,
    config={"access-key": "minio", "secret-key": "minio-secret-key"},
)
RESOURCE_DISPATCHER = CharmSpec(charm="resource-dispatcher", channel="latest/edge", trust=True)
