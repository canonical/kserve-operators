# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures for keda unit tests (ops.testing / Scenario API)."""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from lightkube import ApiError
from ops.testing import Container, Context, Relation, State

from charm import (
    METRICS_APISERVER_CONTAINER,
    OPERATOR_CONTAINER,
    WEBHOOKS_CONTAINER,
    KedaCharm,
)


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


@pytest.fixture(autouse=True)
def mock_gen_certs():
    """Avoid generating real certificates on every test."""
    with patch("charm.gen_certs", return_value={"cert": "c", "key": "k", "ca": "a"}) as m:
        yield m


@pytest.fixture(autouse=True)
def mock_load_generic_resources():
    """Avoid talking to the apiserver during generic-resource registration."""
    with patch("charm.load_in_cluster_generic_resources") as m:
        yield m


@pytest.fixture(autouse=True)
def mock_krh_lightkube_client():
    """Force the chisme KubernetesResourceHandler to use a fake lightkube client."""
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


@pytest.fixture(autouse=True)
def mock_krh_render_manifests():
    """Skip expensive Jinja rendering of upstream YAML templates."""
    sentinel = [MagicMock(name="fake_manifest")]
    with patch.object(KubernetesResourceHandler, "render_manifests", return_value=sentinel) as m:
        yield m


@pytest.fixture
def ctx():
    """A scenario ``Context`` configured for the keda charm."""
    return Context(charm_type=KedaCharm, app_trusted=True)


@pytest.fixture
def containers():
    """All three keda pebble containers, reachable by default."""
    return [
        Container(name=OPERATOR_CONTAINER, can_connect=True),
        Container(name=METRICS_APISERVER_CONTAINER, can_connect=True),
        Container(name=WEBHOOKS_CONTAINER, can_connect=True),
    ]


@pytest.fixture
def base_state(containers):
    """Leader unit with all three containers reachable, no relations."""
    return State(leader=True, containers=containers)


@pytest.fixture
def state_operator_disconnected():
    """Leader unit where the operator container is not yet reachable."""
    return State(
        leader=True,
        containers=[
            Container(name=OPERATOR_CONTAINER, can_connect=False),
            Container(name=METRICS_APISERVER_CONTAINER, can_connect=True),
            Container(name=WEBHOOKS_CONTAINER, can_connect=True),
        ],
    )


@pytest.fixture
def consumer_relation():
    """A consumer relation on the ``keda`` readiness endpoint."""
    return Relation(
        endpoint="keda",
        interface="keda-sync",
        remote_app_name="llm-integrator",
        remote_app_data={},
    )
