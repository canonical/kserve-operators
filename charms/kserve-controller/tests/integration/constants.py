# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import lightkube.codecs
import lightkube.generic_resource
import yaml
from charmed_kubeflow_chisme.testing import generate_container_securitycontext_map

CUSTOM_IMAGES_PATH = Path("./src/default-custom-images.json")
YAMLS_PREFIX = "./tests/integration/crs/"
MANIFESTS_SUFFIX = "-s3"
METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]
CONFIGMAP_NAME = "inferenceservice-config"
PODDEFAULTS_CRD_TEMPLATE = "./tests/integration/crds/poddefaults.yaml"
CONTAINERS_SECURITY_CONTEXT_MAP = generate_container_securitycontext_map(METADATA)

# ConfigMap
CONFIGMAP_TEMPLATE_PATH = Path("./src/templates/configmap_manifests.yaml.j2")
CONFIGMAP_DATA_INGRESS_DOMAIN = "example.com"

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

# Sklearn ISVC backed by an S3 object storage model
S3_MODEL_URL = (
    "https://storage.googleapis.com/kfserving-examples/models/sklearn/1.0/model/model.joblib"
)
S3_BUCKET = "kserve-models"
S3_MODEL_KEY = "sklearn/model.joblib"
SKLEARN_S3_INF_SVC_YAML = yaml.safe_load(Path(YAMLS_PREFIX + "sklearn-iris-s3.yaml").read_text())
SKLEARN_S3_INF_SVC_OBJECT = lightkube.codecs.load_all_yaml(yaml.dump(SKLEARN_S3_INF_SVC_YAML))[0]
SKLEARN_S3_INF_SVC_NAME = SKLEARN_S3_INF_SVC_OBJECT.metadata.name
