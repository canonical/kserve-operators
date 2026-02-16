# Copyright 2025 Canonical Ltd.
"""Charms dependencies for tests."""

from charmed_kubeflow_chisme.testing import CharmSpec

ISTIO_GATEWAY = CharmSpec(
    charm="istio-gateway", channel="latest/edge", config={"kind": "ingress"}, trust=True
)
ISTIO_PILOT = CharmSpec(charm="istio-pilot", channel="latest/edge", trust=True)
KNATIVE_OPERATOR = CharmSpec(charm="knative-operator", channel="1.16/stable", trust=True)
KNATIVE_SERVING = CharmSpec(charm="knative-serving", channel="1.16/stable", trust=True)
METACONTROLLER_OPERATOR = CharmSpec(
    charm="metacontroller-operator", channel="4.11/stable", trust=True
)
MINIO = CharmSpec(
    charm="minio",
    channel="ckf-1.10/stable",
    trust=True,
    config={"access-key": "minio", "secret-key": "minio-secret-key"},
)
RESOURCE_DISPATCHER = CharmSpec(charm="resource-dispatcher", channel="2.0/stable", trust=True)
