# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared constants for the keda charm integration tests."""

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

# KEDA names the HorizontalPodAutoscaler it generates keda-hpa-<scaledobject>.
HPA_NAME_PREFIX = "keda-hpa-"

# Admission-webhook test: a second ScaledObject on the same target must be denied.
WEBHOOK_DEPLOYMENT = "keda-itest-webhook"
WEBHOOK_SCALEDOBJECT_A = "keda-itest-webhook-a"
WEBHOOK_SCALEDOBJECT_B = "keda-itest-webhook-b"

# ScaledJob test: a cron-driven ScaledJob should spawn Jobs up to its max.
SCALEDJOB_NAME = "keda-itest-scaledjob"
SCALEDJOB_MAX_REPLICAS = 2
# KEDA labels the Jobs it owns with this key set to the ScaledJob name.
SCALEDJOB_LABEL = "scaledjob.keda.sh/name"

# watch-namespace config test: KEDA must ignore ScaledObjects outside its scope.
UNWATCHED_NAMESPACE = "keda-itest-unwatched"
SECOND_WATCHED_NAMESPACE = "keda-itest-watched-2"
WATCHED_WORKLOAD = "keda-itest-watched"
WATCHED_SCALEDOBJECT = "keda-itest-watched"
UNWATCHED_WORKLOAD = "keda-itest-unwatched-wl"
UNWATCHED_SCALEDOBJECT = "keda-itest-unwatched-so"
WATCH_NS_MAX_REPLICAS = 2

# metrics-api scaler test: an in-cluster agnhost server echoes a JSON metric that
# a metrics-api ScaledObject reads to drive a workload up, down and to zero. The
# metric value is embedded in the echo URL, so we retune it by re-applying the
# ScaledObject rather than restarting the source.
METRIC_SRC_NAME = "keda-itest-metric-src"
METRIC_SRC_IMAGE = "registry.k8s.io/e2e-test-images/agnhost:2.47"
METRIC_SRC_PORT = 8080
METRICS_API_DEPLOYMENT = "keda-itest-metricsapi"
METRICS_API_SCALEDOBJECT = "keda-itest-metricsapi"
METRICS_API_MAX_REPLICAS = 3
METRICS_API_TARGET_VALUE = "10"
