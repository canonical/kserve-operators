# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared constants for the keda charm integration tests."""

from pathlib import Path

import yaml

# Derive the app name and OCI image resources from the charm metadata so they
# never drift from the packaging.
METADATA = yaml.safe_load((Path(__file__).resolve().parents[3] / "metadata.yaml").read_text())

APP_NAME = METADATA["name"]

IMAGE_RESOURCES = {
    name: data["upstream-source"]
    for name, data in METADATA.get("resources", {}).items()
    if data.get("type") == "oci-image"
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
# Poll the unwatched workload this many times, this far apart, to confirm KEDA
# never scales it.
UNWATCHED_STABILITY_CHECKS = 4
UNWATCHED_STABILITY_INTERVAL_SECONDS = 5

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

# Observability integration test: standalone Prometheus and Loki exercise the
# metrics-endpoint and logging relations without a full COS stack.
PROMETHEUS_CHARM = "prometheus-k8s"
PROMETHEUS_APP = "prometheus"
PROMETHEUS_CHANNEL = "1/stable"
PROMETHEUS_PORT = 9090
LOKI_CHARM = "loki-k8s"
LOKI_APP = "loki"
LOKI_CHANNEL = "1/stable"
LOKI_PORT = 3100
# The three plain-HTTP /metrics ports Prometheus should be scraping (operator,
# metrics-apiserver, admission-webhooks).
EXPECTED_METRICS_PORTS = (8080, 8082, 8086)
