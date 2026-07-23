# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures for lws-controller unit tests.

The tests use the modern ``ops.testing`` (Scenario) API: each test composes an
input ``State`` and runs an event through a ``Context`` to obtain the output
state. The fixtures here provide reusable building blocks (containers,
relations, base states) and a small set of autouse mocks that stub the
unavoidable I/O paths (Kubernetes API client, certificate generation, k8s
Service patcher) so the suite can run without a cluster.
"""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from lightkube import ApiError
from ops.testing import Container, Context, Relation, State

from charm import LWS_SYNC_RELATION, LWSControllerCharm


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
    with patch(
        "charm.gen_certs",
        return_value={"cert": "c", "key": "k", "ca": "a"},
    ) as m:
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
    # By default, simulate that resources queried during removal are already
    # gone so ``ensure_resource_is_deleted`` returns immediately instead of
    # retrying (which would otherwise block the suite for the deletion timeout).
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
def lws_charm():
    """The charm class under test."""
    return LWSControllerCharm


@pytest.fixture
def ctx(lws_charm):
    """A scenario ``Context`` configured for the lws-controller charm."""
    return Context(charm_type=lws_charm, app_trusted=True)


@pytest.fixture
def controller_container():
    """lws-controller pebble container, reachable by default."""
    return Container(name="lws-controller", can_connect=True)


@pytest.fixture
def controller_container_disconnected():
    """lws-controller pebble container with can_connect=False."""
    return Container(name="lws-controller", can_connect=False)


@pytest.fixture
def base_state(controller_container):
    """Leader unit with the controller container reachable, no relations."""
    return State(leader=True, containers=[controller_container])


@pytest.fixture
def consumer_relation():
    """A peer/consumer relation on the lws-controller endpoint."""
    return Relation(
        endpoint=LWS_SYNC_RELATION,
        interface="lws-controller-sync",
        remote_app_name="kserve-llmisvc",
        remote_app_data={},
    )
