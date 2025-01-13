# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

from unittest.mock import MagicMock, patch

import ops.testing
import pytest
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus
from lightkube import ApiError
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from ops.testing import Harness
from serialized_data_interface import SerializedDataInterface

from charm import KServeControllerCharm
from tests.test_data.manifests import MANIFESTS_TEST_DATA

# enable simulation of container networking
ops.testing.SIMULATE_CAN_CONNECT = True

KSERVE_CONTROLLER_EXPECTED_LAYER = {
    "services": {
        "kserve-controller": {
            "command": "/manager --metrics-addr=:8080",
            "environment": {"POD_NAMESPACE": None, "SECRET_NAME": "kserve-webhook-server-cert"},
            "override": "replace",
            "startup": "enabled",
            "summary": "KServe Controller",
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
    harness.set_can_connect("kserve-controller", True)
    with patch("charm.ServicePort"), patch("charm.KubernetesServicePatch"):
        yield harness


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


def test_metrics(harness):
    """Test MetricsEndpointProvider initialization."""
    with patch("charm.MetricsEndpointProvider") as mock_metrics, patch(
        "charm.KubernetesServicePatch"
    ) as mock_service_patcher, patch("charm.ServicePort") as mock_service_port:
        harness.begin()
        mock_metrics.assert_called_once_with(
            harness.charm,
            jobs=[{"static_configs": [{"targets": ["*:8080"]}]}],
        )
        mock_service_port.assert_called_once_with(
            port=8080, targetPort=8080, name="kserve-controller-metrics"
        )
        mock_service_patcher.assert_called_once_with(
            harness.charm, [mock_service_port.return_value], service_name="kserve-controller"
        )


def test_log_forwarding(harness):
    """Test LogForwarder initialization."""
    with patch("charm.LogForwarder") as mock_logging:
        harness.begin()
        mock_logging.assert_called_once_with(charm=harness.charm)


def test_events(harness, mocked_resource_handler, mocker):
    harness.begin()
    on_event = mocker.patch("charm.KServeControllerCharm._on_event")
    on_remove = mocker.patch("charm.KServeControllerCharm._on_remove")

    harness.charm.on.install.emit()
    on_event.assert_called_once()

    harness.charm.on.remove.emit()
    on_remove.assert_called_once()

    on_event.reset_mock()
    harness.charm.on.kserve_controller_pebble_ready.emit("kserve-controller")
    on_event.assert_called_once()


def test_on_install_active(harness, mocked_resource_handler):
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler
    harness.charm._cm_resource_handler = mocked_resource_handler
    harness.charm.on.install.emit()
    mocked_resource_handler.apply.assert_called()
    assert harness.charm.model.unit.status == ActiveStatus()


def test_on_install_exception(harness, mocked_resource_handler, mocker):
    mocked_logger = mocker.patch("charm.log")
    harness.begin()
    mocked_resource_handler.apply.side_effect = _FakeApiError()
    harness.charm._k8s_resource_handler = mocked_resource_handler
    harness.charm._cm_resource_handler = mocked_resource_handler
    with pytest.raises(ApiError):
        harness.charm.on.install.emit()
    mocked_logger.error.assert_called()


def test_on_kserve_controller_ready_active(harness, mocked_resource_handler, mocker):
    harness.begin()

    # Check initial plan is empty
    initial_plan = harness.get_container_pebble_plan("kserve-controller")
    assert initial_plan.to_yaml() == "{}\n"

    # Add relation with ingress-gateway providers
    relation_id_ingress = harness.add_relation("ingress-gateway", "test-istio-pilot")
    relation_id_local = harness.add_relation("local-gateway", "test-knative-serving")

    # Updated the data bag with ingress-gateway
    remote_ingress_data = {
        "gateway_name": "test-ingress-name",
        "gateway_namespace": "test-ingress-namespace",
    }
    remote_local_data = {
        "gateway_name": "test-local-name",
        "gateway_namespace": "test-local-namespace",
    }
    harness.update_relation_data(relation_id_ingress, "test-istio-pilot", remote_ingress_data)
    harness.update_relation_data(relation_id_local, "test-knative-serving", remote_local_data)

    # Check layer gets created
    assert harness.get_container_pebble_plan("kserve-controller")._services != {}

    updated_plan = harness.get_container_pebble_plan("kserve-controller").to_dict()
    assert KSERVE_CONTROLLER_EXPECTED_LAYER == updated_plan

    service = harness.model.unit.get_container("kserve-controller").get_service(
        "kserve-controller"
    )
    assert service.is_running() is True

    assert harness.model.unit.status == ActiveStatus()


def test_on_kserve_controller_ready_no_relation_blocked(harness, mocked_resource_handler, mocker):
    """Tests that charm goes to blocked when it has no relation to knative-serving."""
    harness.begin()

    # Add relation with ingress-gateway providers
    relation_id_ingress = harness.add_relation("ingress-gateway", "test-istio-pilot")

    # Updated the data bag with ingress-gateway
    remote_ingress_data = {
        "gateway_name": "test-ingress-name",
        "gateway_namespace": "test-ingress-namespace",
    }
    harness.update_relation_data(relation_id_ingress, "test-istio-pilot", remote_ingress_data)

    assert harness.model.unit.status == BlockedStatus(
        "Please relate to knative-serving:local-gateway"
    )


def test_on_remove_success(harness, mocker, mocked_resource_handler):
    mocked_delete_many = mocker.patch("charm.delete_many")
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler
    harness.charm._cm_resource_handler = mocked_resource_handler
    harness.charm.on.remove.emit()
    mocked_delete_many.assert_called()
    assert isinstance(harness.charm.model.unit.status, MaintenanceStatus)


def test_on_remove_failure(harness, mocker, mocked_resource_handler):
    harness.begin()

    mocked_delete_many = mocker.patch("charm.delete_many")
    mocked_delete_many.side_effect = _FakeApiError()
    mocked_logger = mocker.patch("charm.log")

    harness.charm._k8s_resource_handler = mocked_resource_handler
    harness.charm._cm_resource_handler = mocked_resource_handler

    with pytest.raises(ApiError):
        harness.charm.on.remove.emit()
    mocked_logger.warning.assert_called()


def test_generate_gateways_context_raw_mode_no_relation(harness, mocker, mocked_resource_handler):
    """Assert the unit gets blocked if no relation."""
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler
    harness.charm.on.install.emit()
    assert harness.charm.model.unit.status == BlockedStatus(
        "Please relate to istio-pilot:gateway-info"
    )


@patch("charm.KServeControllerCharm._restart_controller_service")
def test_generate_gateways_context_serverless_no_relation(
    _mocked_restart_controller_service,
    harness,
    mocker,
    mocked_resource_handler,
):
    """Assert the unit gets blocked if no relation."""
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler

    # Change deployment-mode to serverless
    harness.update_config({"deployment-mode": "serverless"})

    # Add only ingress-gateway relation
    relation_id_ingress = harness.add_relation("ingress-gateway", "test-istio-pilot")
    remote_ingress_data = {
        "gateway_name": "test-ingress-name",
        "gateway_namespace": "test-ingress-namespace",
    }
    harness.update_relation_data(relation_id_ingress, "test-istio-pilot", remote_ingress_data)

    harness.charm.on.install.emit()
    assert harness.charm.model.unit.status == BlockedStatus(
        "Please relate to knative-serving:local-gateway"
    )


@pytest.mark.parametrize(
    "remote_data", ({"gateway_name": "test-name"}, {"gateway_namespace": "test-namespace"})
)
def test_generate_gateways_context_raw_mode_missing_data(
    remote_data, harness, mocker, mocked_resource_handler
):
    """Assert the unit goes to waiting status if there is incomplete data."""
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler

    # Add relation with ingress-gateway provider, in the case of kserve it will
    # always be istio-pilot
    relation_id = harness.add_relation("ingress-gateway", "test-istio-pilot")

    # Updated the data bag with ingress-gateway
    harness.update_relation_data(relation_id, "test-istio-pilot", remote_data)

    assert harness.charm.model.unit.status == WaitingStatus("Waiting for ingress gateway data.")


@pytest.mark.parametrize(
    "remote_data", ({"gateway_name": "test-name"}, {"gateway_namespace": "test-namespace"})
)
@patch("charm.KServeControllerCharm._restart_controller_service")
def test_generate_gateways_context_serverless_missing_data(
    _mocked_restart_controller_service,
    remote_data,
    harness,
    mocker,
    mocked_resource_handler,
):
    """Assert the unit goes to waiting status if there is incomplete data."""
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler

    # Change deployment-mode to serverless
    harness.update_config({"deployment-mode": "serverless"})

    # Add ingress-gateway relation
    relation_id_ingress = harness.add_relation("ingress-gateway", "test-istio-pilot")
    remote_ingress_data = {
        "gateway_name": "test-ingress-name",
        "gateway_namespace": "test-ingress-namespace",
    }
    harness.update_relation_data(relation_id_ingress, "test-istio-pilot", remote_ingress_data)

    # Add relation with ingress-gateway provider, in the case of kserve it will
    # always be istio-pilot
    relation_id_local = harness.add_relation("local-gateway", "test-knative-serving")

    # Updated the data bag with ingress-gateway
    harness.update_relation_data(relation_id_local, "test-knative-serving", remote_data)

    assert harness.charm.model.unit.status == WaitingStatus("Waiting for local gateway data.")


def test_generate_gateways_context_raw_mode_pass(harness, mocker, mocked_resource_handler):
    """Assert the gateway context is correct."""
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler

    # Add relation with ingress-gateway provider, in the case of kserve it will
    # always be istio-pilot
    relation_id = harness.add_relation("ingress-gateway", "test-istio-pilot")

    # Updated the data bag with ingress-gateway
    remote_data = {"gateway_name": "test-name", "gateway_namespace": "test-namespace"}
    harness.update_relation_data(relation_id, "test-istio-pilot", remote_data)

    # Get relation data
    ingress_gateway_info = harness.get_relation_data(relation_id, "test-istio-pilot")
    # Compare actual and expected gateways context
    expected_gateway_context = {
        "ingress_gateway_name": "test-name",
        "ingress_gateway_namespace": "test-namespace",
        "ingress_gateway_service_name": "istio-ingressgateway-workload",
        "local_gateway_name": "",
        "local_gateway_namespace": "",
        "local_gateway_service_name": "",
    }
    actual_gateway_context = {
        "ingress_gateway_name": ingress_gateway_info["gateway_name"],
        "ingress_gateway_namespace": ingress_gateway_info["gateway_namespace"],
        "ingress_gateway_service_name": "istio-ingressgateway-workload",
        "local_gateway_name": "",
        "local_gateway_namespace": "",
        "local_gateway_service_name": "",
    }
    assert actual_gateway_context == expected_gateway_context


@patch("charm.KServeControllerCharm._restart_controller_service")
def test_generate_gateways_context_serverless_mode_pass(
    _mocked_restart_controller_service,
    harness,
    mocker,
    mocked_resource_handler,
):
    """Assert the gateway context is correct."""
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler

    # Change deployment-mode to serverless
    harness.update_config({"deployment-mode": "serverless"})

    # Add relation with ingress-gateway providers
    relation_id_ingress = harness.add_relation("ingress-gateway", "test-istio-pilot")
    relation_id_local = harness.add_relation("local-gateway", "test-knative-serving")

    # Updated the data bag with ingress-gateway
    remote_ingress_data = {
        "gateway_name": "test-ingress-name",
        "gateway_namespace": "test-ingress-namespace",
    }
    remote_local_data = {
        "gateway_name": "test-local-name",
        "gateway_namespace": "test-local-namespace",
    }
    harness.update_relation_data(relation_id_ingress, "test-istio-pilot", remote_ingress_data)
    harness.update_relation_data(relation_id_local, "test-knative-serving", remote_local_data)

    # Get relation data
    ingress_gateway_info = harness.get_relation_data(relation_id_ingress, "test-istio-pilot")
    local_gateway_info = harness.get_relation_data(relation_id_local, "test-knative-serving")
    # Compare actual and expected gateways context
    expected_gateway_context = {
        "ingress_gateway_name": "test-ingress-name",
        "ingress_gateway_namespace": "test-ingress-namespace",
        "ingress_gateway_service_name": "istio-ingressgateway-workload",
        "local_gateway_name": "test-local-name",
        "local_gateway_namespace": "test-local-namespace",
        "local_gateway_service_name": "knative-local-gateway",
    }
    actual_gateway_context = {
        "ingress_gateway_name": ingress_gateway_info["gateway_name"],
        "ingress_gateway_namespace": ingress_gateway_info["gateway_namespace"],
        "ingress_gateway_service_name": "istio-ingressgateway-workload",
        "local_gateway_name": local_gateway_info["gateway_name"],
        "local_gateway_namespace": local_gateway_info["gateway_namespace"],
        "local_gateway_service_name": "knative-local-gateway",
    }
    assert actual_gateway_context == expected_gateway_context


def test_get_certs(harness, mocker, mocked_resource_handler):
    """Test certs generation."""
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler

    cert_attributes = ["cert", "ca", "key"]

    # obtain certs and verify contents
    for attr in cert_attributes:
        assert hasattr(harness.charm._stored, attr)


@pytest.mark.parametrize(
    "cert_data_dict, should_certs_refresh",
    [
        # Cases where we should generate a new cert
        # No cert data, we should refresh certs
        ({}, True),
        # We are missing one of the required cert data fields, we should refresh certs
        ({"ca": "x", "key": "x"}, True),
        ({"cert": "x", "key": "x"}, True),
        ({"cert": "x", "ca": "x"}, True),
        # Cases where we should not generate a new cert
        # Cert data already exists, we should not refresh certs
        (
            {
                "cert": "x",
                "ca": "x",
                "key": "x",
            },
            False,
        ),
    ],
)
def test_gen_certs_if_missing(cert_data_dict, should_certs_refresh, harness: Harness, mocker):
    """Test _gen_certs_if_missing.
    This tests whether _gen_certs_if_missing:
    * generates a new cert if there is no existing one
    * does not generate a new cert if there is an existing one
    """
    # Arrange
    # Mock away gen_certs so the class does not generate any certs unless we want it to
    mocked_gen_certs = mocker.patch("charm.KServeControllerCharm._gen_certs", autospec=True)
    harness.begin()
    mocked_gen_certs.reset_mock()

    # Set any provided cert data to _stored
    for k, v in cert_data_dict.items():
        setattr(harness.charm._stored, k, v)

    # Act
    harness.charm._gen_certs_if_missing()

    # Assert that we have/have not called refresh_certs, as expected
    assert mocked_gen_certs.called == should_certs_refresh


def test_restart_controller_service(harness, mocked_resource_handler, mocker):
    """Checks the controller service is restarted correctly."""
    harness.begin()

    # Before pebble ready, the service should not be
    # there, so no action should be taken
    harness.charm._restart_controller_service()
    controller_pebble_plan = harness.get_container_pebble_plan(
        harness.charm._controller_container_name
    )
    controller_service = controller_pebble_plan.services.get(
        harness.charm._controller_container_name
    )
    assert controller_service is None

    # Add relation with ingress-gateway providers
    relation_id_ingress = harness.add_relation("ingress-gateway", "test-istio-pilot")
    relation_id_local = harness.add_relation("local-gateway", "test-knative-serving")

    # Updated the data bag with ingress-gateway
    remote_ingress_data = {
        "gateway_name": "test-ingress-name",
        "gateway_namespace": "test-ingress-namespace",
    }
    remote_local_data = {
        "gateway_name": "test-local-name",
        "gateway_namespace": "test-local-namespace",
    }
    harness.update_relation_data(relation_id_ingress, "test-istio-pilot", remote_ingress_data)
    harness.update_relation_data(relation_id_local, "test-knative-serving", remote_local_data)

    # Simulate what happens after the pebble ready event
    harness.container_pebble_ready(harness.charm._controller_container_name)
    mocked_container_restart = mocker.patch.object(harness.charm.controller_container, "restart")
    harness.charm._restart_controller_service()
    mocked_container_restart.assert_called_once()


@pytest.mark.parametrize(
    "context, test_file, expected",
    MANIFESTS_TEST_DATA,
)
def test_create_manifests(context, test_file, expected, harness: Harness):
    """Tests manifests are properly created from context data"""
    harness.begin()
    manifests = harness.charm._create_manifests(test_file, context)
    assert manifests == expected


def test_validate_sdi_interface_default_return(harness: Harness):
    """Test default value is returned on missing interface"""
    interfaces = {}
    default_value = {}
    relation_name = "test_relation"
    harness.begin()
    result = harness.charm._validate_sdi_interface(interfaces, relation_name, default_value)
    assert result == default_value


def test_validate_sdi_interface_success(harness: Harness):
    """Test data bag is extracted correctly from relation"""
    expected_data = {"access-key": "test"}
    storage_object = MagicMock(spec=SerializedDataInterface)
    storage_object.get_data.return_value = {"data": expected_data}
    relation_name = "object-storage"
    interfaces = {relation_name: storage_object}
    harness.begin()
    result = harness.charm._validate_sdi_interface(interfaces, relation_name, "")
    assert result == expected_data
