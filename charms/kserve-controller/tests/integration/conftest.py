# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

import lightkube
import pytest
import tenacity
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Namespace

from tests.integration.constants import (
    TESTING_NAMESPACE_NAME,
)

logger = logging.getLogger(__name__)


@pytest.fixture(scope="session")
def lightkube_client() -> lightkube.Client:
    client = lightkube.Client(field_manager="kserve")
    return client


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

    yield TESTING_NAMESPACE_NAME

    # Cleanup
    lightkube_client.delete(Namespace, name=TESTING_NAMESPACE_NAME)
