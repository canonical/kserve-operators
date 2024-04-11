# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
"""Constants module including constants used in tests."""
from pathlib import Path

import yaml

MANIFESTS_SUFFIX = "-s3"
METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
METACONTROLLER = "metacontroller-operator"
METACONTROLLER_CHANNEL = "3.0/stable"
METACONTROLLER_TRUST = True
MINIO = "minio"
MINIO_CHANNEL = "ckf-1.8/stable"
MINIO_CONFIG = {
    "access-key": "minio",
    "secret-key": "minio123",
    "port": "9000",
}
RESOURCE_DISPATCHER = "resource-dispatcher"
RESOURCE_DISPATCHER_CHANNEL = "1.0/stable"
RESOURCE_DISPATCHER_TRUST = True
ISTIO_CHANNEL = "1.17/stable"
ISTIO_PILOT = "istio-pilot"
ISTIO_PILOT_TRUST = True
ISTIO_GATEWAY = "istio-gateway"
ISTIO_GATEWAY_APP_NAME = "istio-ingressgateway"
ISTIO_GATEWAY_TRUST = True
KNATIVE_CHANNEL = "1.10/stable"
KNATIVE_OPERATOR = "knative-operator"
KNATIVE_OPERATOR_TRUST = True
KNATIVE_SERVING = "knative-serving"
KNATIVE_SERVING_TRUST = True
CHARM_NAME = METADATA["name"]
NAMESPACE_FILE = "./tests/integration/namespace.yaml"
TESTING_LABELS = ["user.kubeflow.org/enabled"]
ISTIO_INGRESS_GATEWAY = "test-gateway"
METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]
CONFIGMAP_NAME = "inferenceservice-config"
EXPECTED_CONFIGMAP = yaml.safe_load(Path("./tests/integration/config-map-data.yaml").read_text())
EXPECTED_CONFIGMAP_CHANGED = yaml.safe_load(
    Path("./tests/integration/config-map-data-changed.yaml").read_text()
)
PODDEFAULTS_CRD_TEMPLATE = "./tests/integration/crds/poddefaults.yaml"
TESTING_NAMESPACE_NAME = "raw-deployment"
KSERVE_WORKLOAD_CONTAINER = "kserve-container"
