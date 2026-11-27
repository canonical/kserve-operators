# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
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

# Shared bucket holding the test models
MODELS_S3_BUCKET = "charmed-kubeflow-llm-storage"
MODELS_S3_PREFIX = "kserve-controller-models"
MODELS_S3_REGION = "eu-central-1"
MODELS_S3_ENDPOINT = f"s3.{MODELS_S3_REGION}.amazonaws.com"
MODELS_S3_SA_NAME = "kserve-test-models-s3"
MODELS_S3_SECRET_NAME = "kserve-test-models-s3"
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
SKLEARN_S3_SOURCE_KEY = f"{MODELS_S3_PREFIX}/sklearn/model/model.joblib"

S3_BUCKET = "kserve-models"
S3_MODEL_KEY = "sklearn/model.joblib"
SKLEARN_S3_INF_SVC_YAML = yaml.safe_load(Path(YAMLS_PREFIX + "sklearn-iris-s3.yaml").read_text())
SKLEARN_S3_INF_SVC_OBJECT = lightkube.codecs.load_all_yaml(yaml.dump(SKLEARN_S3_INF_SVC_YAML))[0]
SKLEARN_S3_INF_SVC_NAME = SKLEARN_S3_INF_SVC_OBJECT.metadata.name
