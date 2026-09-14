# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared constants for the keda charm integration tests."""

from lightkube.generic_resource import create_namespaced_resource

APP_NAME = "keda-controller"

IMAGE_RESOURCES = {
    "keda-operator-image": "ghcr.io/kedacore/keda:2.17.3",
    "keda-metrics-apiserver-image": "ghcr.io/kedacore/keda-metrics-apiserver:2.17.3",
    "keda-admission-webhooks-image": "ghcr.io/kedacore/keda-admission-webhooks:2.17.3",
}

# CRDs the charm installs.
KEDA_CRDS = (
    "scaledobjects.keda.sh",
    "scaledjobs.keda.sh",
    "triggerauthentications.keda.sh",
    "clustertriggerauthentications.keda.sh",
    "cloudeventsources.eventing.keda.sh",
    "clustercloudeventsources.eventing.keda.sh",
)

# The cluster-scoped external-metrics APIService the charm registers.
EXTERNAL_METRICS_APISERVICE = "v1beta1.external.metrics.k8s.io"

# A throwaway workload the cron ScaledObject scales during the smoke test.
SMOKE_DEPLOYMENT = "keda-itest-smoke"
SMOKE_SCALEDOBJECT = "keda-itest-smoke"
SMOKE_IMAGE = "registry.k8s.io/pause:3.9"
SMOKE_MAX_REPLICAS = 2

ScaledObject = create_namespaced_resource(
    group="keda.sh",
    version="v1alpha1",
    kind="ScaledObject",
    plural="scaledobjects",
)
