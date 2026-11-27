# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

import lightkube
import pytest
import tenacity
from _pytest.config.argparsing import Parser
from lightkube.models.core_v1 import ObjectReference
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Namespace, Secret, ServiceAccount

from tests.integration.constants import (
    AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY,
    MODELS_S3_ENDPOINT,
    MODELS_S3_REGION,
    MODELS_S3_SA_NAME,
    MODELS_S3_SECRET_NAME,
)

TESTING_NAMESPACE_NAME = "kserve-testing"

logger = logging.getLogger(__name__)


def pytest_addoption(parser: Parser):
    parser.addoption(
        "--charm-path",
        help="Path to charm file for performing tests on.",
    )


# Fail fast (rather than hang in Init) if the credentials for the test models are missing.
@pytest.fixture(scope="session", autouse=True)
def require_aws_credentials():
    if not (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY):
        pytest.fail(
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set to fetch the test "
            "models from S3; export them locally or provide them via CI secrets.",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def lightkube_client() -> lightkube.Client:
    client = lightkube.Client(field_manager="kserve")
    return client


def create_models_s3_credentials(lightkube_client: lightkube.Client, namespace: str):
    """Create the Secret and ServiceAccount the InferenceServices use to pull the test models.

    KServe injects the Secret's keys and s3-* annotations into the storage-initializer of
    predictors whose serviceAccountName references this ServiceAccount.
    """
    lightkube_client.create(
        Secret(
            metadata=ObjectMeta(
                name=MODELS_S3_SECRET_NAME,
                namespace=namespace,
                annotations={
                    "serving.kserve.io/s3-endpoint": MODELS_S3_ENDPOINT,
                    "serving.kserve.io/s3-usehttps": "1",
                    "serving.kserve.io/s3-region": MODELS_S3_REGION,
                },
            ),
            type="Opaque",
            stringData={
                "AWS_ACCESS_KEY_ID": AWS_ACCESS_KEY_ID,
                "AWS_SECRET_ACCESS_KEY": AWS_SECRET_ACCESS_KEY,
            },
        )
    )
    lightkube_client.create(
        ServiceAccount(
            metadata=ObjectMeta(name=MODELS_S3_SA_NAME, namespace=namespace),
            secrets=[ObjectReference(name=MODELS_S3_SECRET_NAME)],
        )
    )


@pytest.fixture()
def test_namespace(lightkube_client: lightkube.Client):
    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=15),
        stop=tenacity.stop_after_delay(60 * 5),
        reraise=True,
    )
    def create_namespace():
        ns = Namespace(
            metadata=ObjectMeta(
                name=TESTING_NAMESPACE_NAME,
                labels={"user.kubeflow.org/enabled": "true"},
            )
        )
        lightkube_client.create(ns)

    create_namespace()
    try:
        # Must exist before any InferenceService
        create_models_s3_credentials(lightkube_client, TESTING_NAMESPACE_NAME)
        yield TESTING_NAMESPACE_NAME
    finally:
        # Cleanup (deleting the namespace also removes the Secret and ServiceAccount)
        lightkube_client.delete(Namespace, name=TESTING_NAMESPACE_NAME)
