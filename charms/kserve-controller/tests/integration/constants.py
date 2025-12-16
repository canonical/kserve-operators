# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import lightkube.codecs
import lightkube.generic_resource
import yaml
from charmed_kubeflow_chisme.testing import generate_container_securitycontext_map
from tenacity import Retrying, stop_after_delay, wait_fixed

CUSTOM_IMAGES_PATH = Path("./src/default-custom-images.json")
YAMLS_PREFIX = "./tests/integration/crs/"
MANIFESTS_SUFFIX = "-s3"
METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
NAMESPACE_FILE = YAMLS_PREFIX + "namespace.yaml"
METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]
CONFIGMAP_NAME = "inferenceservice-config"
PODDEFAULTS_CRD_TEMPLATE = "./tests/integration/crds/poddefaults.yaml"
CONTAINERS_SECURITY_CONTEXT_MAP = generate_container_securitycontext_map(METADATA)

TESTING_NAMESPACE_NAME = "kserve-testing"
KSERVE_WORKLOAD_CONTAINER = "kserve-container"

# Serverless
ISTIO_INGRESS_GATEWAY = "test-gateway"
ISTIO_GATEWAY_APP_NAME = "istio-ingressgateway"

# tenacity
RETRY_FOR_THREE_MINUTES = Retrying(
    stop=stop_after_delay(60 * 3),
    wait=wait_fixed(5),
    reraise=True,
)

# ConfigMap
CONFIGMAP_TEMPLATE_PATH = Path("./src/templates/configmap_manifests.yaml.j2")
CONFIGMAP_DATA_INGRESS_DOMAIN = "example.com"
CONFIGMAP_DATA_INGRESS_GATEWAY_NAMESPACE = "kubeflow"

# ConfigMap (Serverless)
CONFIGMAP_DATA_LOCAL_GATEWAY_NAMESPACE = "knative-serving"
CONFIGMAP_DATA_LOCAL_GATEWAY_NAME = "knative-local-gateway"
CONFIGMAP_DATA_LOCAL_GATEWAY_SERVICE_NAME = "knative-local-gateway"
CONFIGMAP_DATA_INGRESS_GATEWAY_NAME_SERVERLESS = "test-gateway"

# Lightkube resources
POD_DEFAULT = lightkube.generic_resource.create_namespaced_resource(
    "kubeflow.org", "v1alpha1", "PodDefault", "poddefaults"
)
ISVC = lightkube.generic_resource.create_namespaced_resource(
    group="serving.kserve.io",
    version="v1beta1",
    kind="InferenceService",
    plural="inferenceservices",
    verbs=None,
)

# Sklearn ISVC
SKLEARN_INF_SVC_YAML = yaml.safe_load(Path(YAMLS_PREFIX + "sklearn-iris.yaml").read_text())
SKLEARN_INF_SVC_OBJECT = lightkube.codecs.load_all_yaml(yaml.dump(SKLEARN_INF_SVC_YAML))[0]
SKLEARN_INF_SVC_NAME = SKLEARN_INF_SVC_OBJECT.metadata.name
