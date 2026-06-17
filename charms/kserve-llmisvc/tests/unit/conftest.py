# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures for kserve-llmisvc unit tests.

The tests use the modern ``ops.testing`` (Scenario) API: each test composes an
input ``State`` and runs an event through a ``Context`` to obtain the output
state. The fixtures here provide reusable building blocks (containers,
relations, base states) and a small set of autouse mocks that stub the
unavoidable I/O paths (Kubernetes API client, certificate generation, k8s
Service patcher) so the suite can run without a cluster.

Tests should assert on the returned ``State`` (statuses, container layers,
opened ports, etc.) rather than poking at charm internals.
"""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from lightkube import ApiError
from ops import pebble
from ops.testing import Container, Context, Relation, State

from charm import (
    CONTROLLER_SYNC_RELATION,
    LWS_SYNC_RELATION,
    METRICS_PROXY_CONTAINER,
    KServeLLMISVCCharm,
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


# ---------------------------------------------------------------------------
# Autouse mocks: stub unavoidable I/O so unit tests can run cluster-less.
# ---------------------------------------------------------------------------


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
def mock_service_patcher():
    """Stub Unit.set_ports so charm init does not call Juju backend in tests."""
    with patch("ops.model.Unit.set_ports") as m:
        yield m


@pytest.fixture(autouse=True)
def mock_krh_lightkube_client():
    """Force the chisme KubernetesResourceHandler to use a fake lightkube client.

    Without this, accessing ``handler.lightkube_client`` would try to build a
    real ``lightkube.Client`` from the in-cluster service account.
    """
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
    """Skip expensive Jinja rendering of upstream YAML templates.

    Returns a single sentinel object per handler so ``_sync_handler_resource_types``
    treats the handler as having manifests (which keeps the remove path exercising
    ``delete``) without paying the per-call Jinja cost.
    """
    sentinel = [MagicMock(name="fake_manifest")]
    with patch.object(KubernetesResourceHandler, "render_manifests", return_value=sentinel) as m:
        yield m


# ---------------------------------------------------------------------------
# Context / charm fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def llmisvc_charm():
    """The charm class under test."""
    return KServeLLMISVCCharm


@pytest.fixture
def ctx(llmisvc_charm):
    """A scenario ``Context`` configured for the kserve-llmisvc charm."""
    return Context(charm_type=llmisvc_charm, app_trusted=True)


# ---------------------------------------------------------------------------
# Container fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def controller_container():
    """llmisvc-controller pebble container, reachable by default."""
    return Container(name="llmisvc-controller", can_connect=True)


@pytest.fixture
def controller_container_disconnected():
    """llmisvc-controller pebble container with can_connect=False."""
    return Container(name="llmisvc-controller", can_connect=False)


@pytest.fixture
def metrics_proxy_container():
    """metrics-proxy pebble container, reachable by default."""
    return Container(name=METRICS_PROXY_CONTAINER, can_connect=True)


@pytest.fixture
def metrics_proxy_container_disconnected():
    """metrics-proxy pebble container with can_connect=False."""
    return Container(name=METRICS_PROXY_CONTAINER, can_connect=False)


@pytest.fixture
def both_containers(controller_container, metrics_proxy_container):
    """Convenience: both pebble containers reachable."""
    return [controller_container, metrics_proxy_container]


# ---------------------------------------------------------------------------
# Relation fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def controller_relation_ready():
    """kserve-controller sync relation with ``ready=true`` published."""
    return Relation(
        endpoint=CONTROLLER_SYNC_RELATION,
        interface="kserve-controller-sync",
        remote_app_name="kserve-controller",
        remote_app_data={"ready": "true"},
    )


@pytest.fixture
def controller_relation_not_ready():
    """kserve-controller sync relation without the readiness flag."""
    return Relation(
        endpoint=CONTROLLER_SYNC_RELATION,
        interface="kserve-controller-sync",
        remote_app_name="kserve-controller",
        remote_app_data={},
    )


@pytest.fixture
def lws_relation_ready():
    """lws-controller sync relation with ``ready=true`` published."""
    return Relation(
        endpoint=LWS_SYNC_RELATION,
        interface="lws-controller-sync",
        remote_app_name="lws-controller",
        remote_app_data={"ready": "true"},
    )


@pytest.fixture
def lws_relation_not_ready():
    """lws-controller sync relation without the readiness flag."""
    return Relation(
        endpoint=LWS_SYNC_RELATION,
        interface="lws-controller-sync",
        remote_app_name="lws-controller",
        remote_app_data={},
    )


# ---------------------------------------------------------------------------
# Pre-built State fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def base_state(both_containers):
    """Leader unit with both containers reachable, no relations."""
    return State(leader=True, containers=both_containers)


@pytest.fixture
def ready_state(both_containers, controller_relation_ready, lws_relation_ready):
    """Leader unit, both containers, both sync relations reporting ready."""
    return State(
        leader=True,
        containers=both_containers,
        relations=[controller_relation_ready, lws_relation_ready],
    )


# ---------------------------------------------------------------------------
# Misc helpers re-exported for tests.
# ---------------------------------------------------------------------------


@pytest.fixture
def pebble_layer_cls():
    """Expose pebble.Layer for tests that build expected layers."""
    return pebble.Layer
