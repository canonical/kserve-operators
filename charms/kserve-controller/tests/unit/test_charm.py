# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

from unittest.mock import MagicMock

import pytest
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus
from lightkube.core.exceptions import ApiError
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus
from ops.testing import Harness

from charm import KServeControllerCharm


RBAC_PROXY_EXPECTED_LAYER = {
    "services": {
        "kube-rbac-proxy": {
            "override": "replace",
            "summary": "Kube Rbac Proxy",
            "command": "/usr/local/bin/kube-rbac-proxy --secure-listen-address=0.0.0.0:8443 --upstream=http://127.0.0.1:8080 --logtostderr=true --v=10",
            "startup": "enabled",
        }
    }
}


class _FakeErrorWithStatus(ErrorWithStatus):
    def __init__(self, status_type=BlockedStatus):
        super().__init__("err", status_type)


class _FakeResponse:
    """Used to fake an httpx response during testing only."""

    def __init__(self, code):
        self.code = code

    def json(self):
        return {"apiVersion": 1, "code": self.code, "message": "broken"}


class _FakeApiError(ApiError):
    """Used to simulate an ApiError during testing."""

    def __init__(self, code=400):
        super().__init__(response=_FakeResponse(code))


@pytest.fixture
def harness():
    """Returns a harnessed charm with leader == True."""
    harness = Harness(KServeControllerCharm)
    harness.set_leader(True)
    return harness


@pytest.fixture()
def mocked_resource_handler(mocker):
    """Yields a mocked instance of the KubernetesResourceHAndler."""
    mocked_resource_handler = MagicMock()
    mocked_resource_handler_factory = mocker.patch("charm.KubernetesResourceHandler")
    mocked_resource_handler_factory.return_value = mocked_resource_handler
    yield mocked_resource_handler


@pytest.fixture()
def mocked_lightkube_client(mocker, mocked_resource_handler):
    """Prevents lightkube clients from being created, returning a mock instead."""
    mocked_resource_handler.lightkube_client = MagicMock()
    yield mocked_resource_handler.lightkube_client


@pytest.fixture()
def mocked_gen_certs(mocker):
    """Yields a mocked gen_certs."""
    yield mocker.patch("charm.KServeControllerCharm.gen_certs")


def test_events(harness, mocked_resource_handler, mocker):
    harness.begin()
    on_install = mocker.patch("charm.KServeControllerCharm._on_install")
    on_remove = mocker.patch("charm.KServeControllerCharm._on_remove")
    on_kserve_controller_pebble_ready = mocker.patch(
        "charm.KServeControllerCharm._on_kserve_controller_ready"
    )
    on_kube_rbac_proxy_ready = mocker.patch(
        "charm.KServeControllerCharm._on_kube_rbac_proxy_ready"
    )

    harness.charm.on.install.emit()
    on_install.assert_called_once()

    harness.charm.on.remove.emit()
    on_remove.assert_called_once()

    harness.charm.on.kserve_controller_pebble_ready.emit("kserve-controller")
    on_kserve_controller_pebble_ready.assert_called_once()

    harness.charm.on.kube_rbac_proxy_pebble_ready.emit("kube-rbac-proxy")
    on_kube_rbac_proxy_ready.assert_called_once()


def test_on_install_active(harness, mocked_resource_handler):
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler
    harness.charm.on.install.emit()
    mocked_resource_handler.apply.assert_called()
    assert harness.charm.model.unit.status == ActiveStatus()


def test_on_install_exception(harness, mocked_resource_handler, mocker):
    mocked_logger = mocker.patch("charm.log")
    harness.begin()
    mocked_resource_handler.apply.side_effect = _FakeApiError()
    harness.charm._k8s_resource_handler = mocked_resource_handler
    with pytest.raises(ApiError):
        harness.charm.on.install.emit()
    mocked_logger.error.assert_called()


def test_on_kube_rbac_proxy_ready_active(harness, mocker):
    harness.begin()

    # Check initial plan is empty
    initial_plan = harness.get_container_pebble_plan("kube-rbac-proxy")
    assert initial_plan.to_yaml() == "{}\n"

    # Check layer gets created
    harness.charm.on.kube_rbac_proxy_pebble_ready.emit("kube-rbac-proxy")
    assert harness.get_container_pebble_plan("kube-rbac-proxy")._services != {}

    updated_plan = harness.get_container_pebble_plan("kube-rbac-proxy").to_dict()
    assert RBAC_PROXY_EXPECTED_LAYER == updated_plan

    service = harness.model.unit.get_container("kube-rbac-proxy").get_service("kube-rbac-proxy")
    assert service.is_running() is True

    assert harness.model.unit.status == ActiveStatus()


def test_on_kube_rbac_proxy_ready_exception_blocked(harness, mocker):
    harness.begin()

    mocked_update_layer = mocker.patch("charm.update_layer")
    mocked_update_layer.side_effect = _FakeErrorWithStatus()
    mocked_logger = mocker.patch("charm.log")

    harness.charm.on.kube_rbac_proxy_pebble_ready.emit("kube-rbac-proxy")
    mocked_logger.error.assert_called_once()
    mocked_logger.info.assert_not_called()


def test_on_kube_rbac_proxy_ready_exception_other(harness, mocker):
    harness.begin()

    mocked_update_layer = mocker.patch("charm.update_layer")
    mocked_update_layer.side_effect = _FakeErrorWithStatus(status_type=MaintenanceStatus)
    mocked_logger = mocker.patch("charm.log")

    harness.charm.on.kube_rbac_proxy_pebble_ready.emit("kube-rbac-proxy")

    mocked_logger.info.assert_called_once()
    mocked_logger.error.assert_not_called()


def test_on_kserve_controller_ready_active(harness, mocker):
    harness.begin()

    # Check initial plan is empty
    initial_plan = harness.get_container_pebble_plan("kserve-controller")
    assert initial_plan.to_yaml() == "{}\n"

    # FIXME: missing mocked ops.model.Container.push
    # Check layer gets created
    # harness.charm.on.kserve_controller_pebble_ready.emit("kserve-controller")
    # assert harness.get_container_pebble_plan("kserve-controller")._services != {}


def test_on_remove_success(harness, mocker, mocked_resource_handler, mocked_gen_certs):
    mocked_delete_many = mocker.patch("charm.delete_many")
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler
    harness.charm.on.remove.emit()
    mocked_delete_many.assert_called()
    assert isinstance(harness.charm.model.unit.status, MaintenanceStatus)


def test_on_remove_failure(harness, mocker, mocked_resource_handler, mocked_gen_certs):
    harness.begin()

    mocked_delete_many = mocker.patch("charm.delete_many")
    mocked_delete_many.side_effect = _FakeApiError()
    mocked_logger = mocker.patch("charm.log")

    harness.charm._k8s_resource_handler = mocked_resource_handler

    with pytest.raises(ApiError):
        harness.charm.on.remove.emit()
    mocked_logger.warning.assert_called()
