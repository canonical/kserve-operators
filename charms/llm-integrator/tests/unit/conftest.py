# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures for llm-integrator unit tests.

The tests use the modern ``ops.testing`` (Scenario) API: each test composes an
input ``State`` and runs an event through a ``Context`` to obtain the output
state. A small set of autouse mocks stub the unavoidable I/O paths (the
Kubernetes API client and the resource-handler apply/delete calls) so the suite
can run without a cluster.
"""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from lightkube import ApiError
from ops.testing import Context, Relation, State

from charm import LLMISVC_SYNC_RELATION, S3_CREDENTIALS_RELATION, LLMIntegratorCharm


class _Fake404Response:
    """Minimal httpx-like response that lightkube parses into a 404 status."""

    code = 404
    message = "not found"

    def json(self):
        return {"apiVersion": 1, "code": 404, "message": "not found"}


class _Fake404ApiError(ApiError):
    """A lightkube ApiError carrying a 404 status, for use as a get() side effect."""

    def __init__(self):
        super().__init__(response=_Fake404Response())


# ---------------------------------------------------------------------------
# Autouse mocks: stub unavoidable I/O so unit tests can run cluster-less.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_krh_lightkube_client():
    """Force the chisme KubernetesResourceHandler to use a fake lightkube client.

    By default ``get`` raises a 404 so the LLMInferenceService is treated as
    not-yet-ready (and, during removal, already gone).
    """
    fake_client = MagicMock(name="fake_lightkube_client")
    fake_client.get.side_effect = _Fake404ApiError()
    with patch.object(
        KubernetesResourceHandler,
        "lightkube_client",
        new_callable=PropertyMock,
        return_value=fake_client,
    ):
        yield fake_client


@pytest.fixture(autouse=True)
def mock_krh_apply():
    """Stub the KubernetesResourceHandler apply call to avoid hitting the API."""
    with patch.object(KubernetesResourceHandler, "apply") as m:
        yield m


@pytest.fixture(autouse=True)
def mock_krh_delete():
    """Stub the KubernetesResourceHandler delete call to avoid hitting the API."""
    with patch.object(KubernetesResourceHandler, "delete") as m:
        yield m


# ---------------------------------------------------------------------------
# Context / state fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """A scenario ``Context`` configured for the llm-integrator charm."""
    return Context(charm_type=LLMIntegratorCharm, app_trusted=True)


@pytest.fixture
def valid_config():
    """A complete, valid charm configuration."""
    return {
        "model-uri": "hf://EleutherAI/pythia-70m",
        "model-name": "pythia-70m",
        "runtime-image": "quay.io/example/vllm-cpu:latest",
    }


@pytest.fixture
def llmisvc_relation_ready():
    """kserve-llmisvc sync relation with ``ready=true`` published."""
    return Relation(
        endpoint=LLMISVC_SYNC_RELATION,
        interface="kserve-llmisvc-sync",
        remote_app_name="kserve-llmisvc",
        remote_app_data={"ready": "true"},
    )


@pytest.fixture
def llmisvc_relation_not_ready():
    """kserve-llmisvc sync relation without the readiness flag."""
    return Relation(
        endpoint=LLMISVC_SYNC_RELATION,
        interface="kserve-llmisvc-sync",
        remote_app_name="kserve-llmisvc",
        remote_app_data={},
    )


@pytest.fixture
def ready_state(valid_config, llmisvc_relation_ready):
    """A leader State with a ready relation and valid config."""
    return State(leader=True, config=valid_config, relations=[llmisvc_relation_ready])


@pytest.fixture
def s3_config():
    """A valid configuration using an s3:// model URI."""
    return {
        "model-uri": "s3://my-bucket/models/pythia-70m",
        "runtime-image": "quay.io/example/vllm-cpu:latest",
    }


@pytest.fixture
def s3_relation():
    """An s3-credentials relation to an s3-integrator."""
    return Relation(
        endpoint=S3_CREDENTIALS_RELATION,
        interface="s3",
        remote_app_name="s3-integrator",
        remote_app_data={"bucket": "my-bucket"},
    )


@pytest.fixture
def mock_s3_connection_info():
    """Stub the s3 connection info so tests need no Juju secrets / cluster.

    Defaults to a complete set of S3 credentials; individual tests can override
    ``return_value`` (e.g. to ``{}``) to exercise the not-ready path.
    """
    with patch.object(LLMIntegratorCharm, "_s3_connection_info") as m:
        m.return_value = {
            "access-key": "AKIAEXAMPLE",
            "secret-key": "secretexample",
            "endpoint": "https://s3.eu-central-1.amazonaws.com",
            "region": "eu-central-1",
            "bucket": "my-bucket",
        }
        yield m
