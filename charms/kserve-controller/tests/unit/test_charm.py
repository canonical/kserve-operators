# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

from unittest.mock import MagicMock, patch

import ops.testing
import pytest
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus
from charmed_service_mesh_helpers.interfaces import GatewayMetadata
from lightkube import ApiError
from ops import StatusBase
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from ops.testing import Harness
from serialized_data_interface import SerializedDataInterface

from charm import KServeControllerCharm, ObjectStillExistsError
from tests.test_data.manifests import MANIFESTS_TEST_DATA

# enable simulation of container networking
ops.testing.SIMULATE_CAN_CONNECT = True

KSERVE_CONTROLLER_EXPECTED_LAYER = {
    "services": {
        "kserve-controller": {
            "command": "/manager --metrics-addr=:8080",
            "environment": {
                "POD_NAMESPACE": None,
                "SECRET_NAME": "kserve-webhook-server-cert",
            },
            "override": "replace",
            "startup": "enabled",
            "summary": "KServe Controller",
            "on-check-failure": {
                "kserve-controller-ready": "restart",
                "kserve-controller-alive": "restart",
            },
        }
    },
    "checks": {
        "kserve-controller-ready": {
            "override": "replace",
            "level": "ready",
            "http": {"url": "http://localhost:8081/readyz"},
        },
        "kserve-controller-alive": {
            "override": "replace",
            "level": "alive",
            "http": {"url": "http://localhost:8081/healthz"},
        },
    },
}


class _FakeObjectStillExistsError(ObjectStillExistsError):
    """Used to simulate an ObjectStillExistsError during testing."""

    def __init__(self, resource_name="a-resource"):
        super().__init__(resource_name=resource_name)


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


def test_metrics(harness: Harness):
    """Test MetricsEndpointProvider initialization."""
    with (
        patch("charm.MetricsEndpointProvider") as mock_metrics,
        patch("charm.KubernetesServicePatch") as mock_service_patcher,
        patch("charm.ServicePort") as mock_service_port,
    ):
        harness.begin()
        mock_metrics.assert_called_once_with(
            harness.charm,
            jobs=[{"static_configs": [{"targets": ["*:8080"]}]}],
        )
        mock_service_port.assert_called_once_with(
            port=8080, targetPort=8080, name="kserve-controller-metrics"
        )
        mock_service_patcher.assert_called_once_with(
            harness.charm,
            [mock_service_port.return_value],
            service_name="kserve-controller",
        )


def test_log_forwarding(harness: Harness):
    """Test LogForwarder initialization."""
    with patch("charm.LogForwarder") as mock_logging:
        harness.begin()
        mock_logging.assert_called_once_with(charm=harness.charm)


def test_events(harness: Harness, mocked_resource_handler, mocker):
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


def test_on_install_active(harness: Harness, mocked_resource_handler):
    harness.begin()
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
    harness.charm._k8s_resource_handler = mocked_resource_handler
    harness.charm._cm_resource_handler = mocked_resource_handler
    harness.charm._cluster_runtimes_resource_handler = mocked_resource_handler
    harness.charm.on.install.emit()
    mocked_resource_handler.apply.assert_called()
    assert harness.charm.model.unit.status == ActiveStatus()


def test_on_install_exception(harness: Harness, mocked_resource_handler, mocker):
    mocked_logger = mocker.patch("charm.log")
    harness.begin()
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
    mocked_resource_handler.apply.side_effect = _FakeApiError()
    harness.charm._k8s_resource_handler = mocked_resource_handler
    harness.charm._cm_resource_handler = mocked_resource_handler
    harness.charm._cluster_runtimes_resource_handler = mocked_resource_handler
    with pytest.raises(ApiError):
        harness.charm.on.install.emit()
    mocked_logger.error.assert_called()


def test_on_kserve_controller_ready_active(harness: Harness, mocked_resource_handler, mocker):
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


def test_on_kserve_controller_ready_no_relation_blocked(
    harness: Harness, mocked_resource_handler, mocker
):
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


def test_on_remove_success(harness: Harness, mocker, mocked_resource_handler):
    mocked_delete_many = mocker.patch("charm.delete_many")
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler
    harness.charm._cm_resource_handler = mocked_resource_handler
    harness.charm._cluster_runtimes_resource_handler = mocked_resource_handler
    harness.charm.on.remove.emit()
    mocked_delete_many.assert_called()
    assert isinstance(harness.charm.model.unit.status, MaintenanceStatus)


def test_on_remove_api_failure(harness: Harness, mocker, mocked_resource_handler):
    harness.begin()

    mocked_delete_many = mocker.patch("charm.delete_many")
    mocked_delete_many.side_effect = _FakeObjectStillExistsError()
    mocked_logger = mocker.patch("charm.log")

    harness.charm._k8s_resource_handler = mocked_resource_handler
    harness.charm._cm_resource_handler = mocked_resource_handler
    harness.charm._cluster_runtimes_resource_handler = mocked_resource_handler

    with pytest.raises(ObjectStillExistsError):
        harness.charm.on.remove.emit()
    mocked_logger.warning.assert_called()


def test_on_remove_deletion_failure(harness: Harness, mocker, mocked_resource_handler):
    harness.begin()

    mocked_delete_many = mocker.patch("charm.delete_many")
    mocked_delete_many.side_effect = _FakeApiError()
    mocked_logger = mocker.patch("charm.log")

    harness.charm._k8s_resource_handler = mocked_resource_handler
    harness.charm._cm_resource_handler = mocked_resource_handler
    harness.charm._cluster_runtimes_resource_handler = mocked_resource_handler

    with pytest.raises(ApiError):
        harness.charm.on.remove.emit()
    mocked_logger.warning.assert_called()


def test_generate_gateways_context_knative_mode_no_relation(
    harness: Harness, mocker, mocked_resource_handler
):
    """Assert the unit gets blocked if no relation."""
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler
    harness.charm.on.install.emit()
    assert harness.charm.model.unit.status == BlockedStatus(
        "Knative mode detected, but no relation to ingress-gateway"
    )


@patch("charm.KServeControllerCharm._restart_controller_service")
def test_generate_gateways_context_knative_no_relation(
    _mocked_restart_controller_service,
    harness: Harness,
    mocker,
    mocked_resource_handler,
):
    """Assert the unit gets blocked if no relation."""
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler

    # Change deployment-mode to knative
    harness.update_config({"deployment-mode": "knative"})

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
    "remote_data",
    ({"gateway_name": "test-name"}, {"gateway_namespace": "test-namespace"}),
)
def test_generate_gateways_context_standard_mode_missing_data(
    remote_data, harness: Harness, mocker, mocked_resource_handler
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
    "remote_data",
    ({"gateway_name": "test-name"}, {"gateway_namespace": "test-namespace"}),
)
@patch("charm.KServeControllerCharm._restart_controller_service")
def test_generate_gateways_context_knative_missing_data(
    _mocked_restart_controller_service,
    remote_data,
    harness: Harness,
    mocker,
    mocked_resource_handler,
):
    """Assert the unit goes to waiting status if there is incomplete data."""
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler

    # Change deployment-mode to knative
    harness.update_config({"deployment-mode": "knative"})

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


def test_generate_gateways_context_standard_mode_pass(
    harness: Harness, mocker, mocked_resource_handler
):
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
def test_generate_gateways_context_knative_mode_pass(
    _mocked_restart_controller_service,
    harness: Harness,
    mocker,
    mocked_resource_handler,
):
    """Assert the gateway context is correct."""
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler

    # Change deployment-mode to knative
    harness.update_config({"deployment-mode": "knative"})

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


def test_get_certs(harness: Harness, mocker, mocked_resource_handler):
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


def test_restart_controller_service(harness: Harness, mocked_resource_handler, mocker):
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


@pytest.mark.parametrize(
    "deployment_mode, ingress_gateway_relation, gateway_metadata_relation, expected_status",
    [
        # Knative, should only work with ingress-gateway relation
        (
            "knative",
            False,
            False,
            BlockedStatus("Knative mode detected, but no relation to ingress-gateway"),
        ),
        (
            "knative",
            False,
            True,
            BlockedStatus("Knative mode detected, but no relation to ingress-gateway"),
        ),
        (
            "knative",
            True,
            True,
            BlockedStatus("Both gateway-metadata and ingress-gateway relations are established"),
        ),
        (
            "knative",
            True,
            False,
            ActiveStatus(),
        ),
        # Standard, can work with either ingress-gateway or gateway-metadata relations
        (
            "standard",
            False,
            False,
            BlockedStatus(
                "Standard mode detected, but no relation to gateway-metadata or ingress-gateway"
            ),
        ),
        (
            "standard",
            False,
            True,
            ActiveStatus(),
        ),
        (
            "standard",
            True,
            True,
            BlockedStatus("Both gateway-metadata and ingress-gateway relations are established"),
        ),
        (
            "standard",
            True,
            False,
            ActiveStatus(),
        ),
    ],
)
def test_deployment_modes_gateway_relations(
    mocked_resource_handler,
    mocker,
    harness: Harness,
    deployment_mode: str,
    ingress_gateway_relation: bool,
    gateway_metadata_relation: bool,
    expected_status: StatusBase,
):
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler

    if gateway_metadata_relation:
        harness.add_relation("gateway-metadata", "istio-ingress-k8s")
        mocker.patch.object(
            harness.charm.gateway_info,
            "get_metadata",
            return_value=(
                GatewayMetadata(
                    namespace="test-namespace",
                    deployment_name="test-deployment",
                    gateway_name="test-gateway",
                    service_account="test-service-account",
                )
            ),
        )

    if ingress_gateway_relation:
        relation_id_ingress = harness.add_relation("ingress-gateway", "istio-pilot")
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
        harness.update_relation_data(relation_id_ingress, "istio-pilot", remote_ingress_data)
        harness.update_relation_data(relation_id_local, "test-knative-serving", remote_local_data)

    harness.update_config({"deployment-mode": deployment_mode})

    assert harness.charm.model.unit.status == expected_status


S3_CREDENTIALS_ACCESS_KEY = "s3-access-key"
S3_CREDENTIALS_SECRET_KEY = "s3-secret-key"
S3_CREDENTIALS_ENDPOINT = "https://my-s3.example.com:9000"
S3_CREDENTIALS_REGION = "eu-west-2"
S3_CREDENTIALS_BUCKET = "my-bucket"

S3_CONNECTION_INFO = {
    "access-key": S3_CREDENTIALS_ACCESS_KEY,
    "secret-key": S3_CREDENTIALS_SECRET_KEY,
    "endpoint": S3_CREDENTIALS_ENDPOINT,
    "region": S3_CREDENTIALS_REGION,
    "bucket": S3_CREDENTIALS_BUCKET,
}

OBJECT_STORAGE_ACCESS_KEY = "minio-access-key"
OBJECT_STORAGE_SECRET_KEY = "minio-secret-key"
OBJECT_STORAGE_SERVICE = "minio"
OBJECT_STORAGE_NAMESPACE = "kubeflow"
OBJECT_STORAGE_PORT = 9000
OBJECT_STORAGE_SECURE = False

OBJECT_STORAGE_DATA = {
    "access-key": OBJECT_STORAGE_ACCESS_KEY,
    "secret-key": OBJECT_STORAGE_SECRET_KEY,
    "service": OBJECT_STORAGE_SERVICE,
    "namespace": OBJECT_STORAGE_NAMESPACE,
    "port": OBJECT_STORAGE_PORT,
    "secure": OBJECT_STORAGE_SECURE,
}


def test_get_storage_secrets_context_object_storage(harness: Harness, mocker):
    """The object-storage relation produces the expected secret context."""
    harness.begin()
    harness.add_relation("object-storage", "minio")
    mocker.patch.object(harness.charm, "_get_interfaces", return_value={})
    mocker.patch.object(harness.charm, "_get_object_storage", return_value=OBJECT_STORAGE_DATA)

    context = harness.charm._get_storage_secrets_context()

    assert context == {
        "secret_name": "kserve-controller-s3",
        "s3_endpoint": "minio.kubeflow:9000",
        "s3_usehttps": "0",
        "s3_region": "us-east-1",
        "s3_useanoncredential": "false",
        "s3_access_key": OBJECT_STORAGE_ACCESS_KEY,
        "s3_secret_access_key": OBJECT_STORAGE_SECRET_KEY,
    }


def test_get_storage_secrets_context_s3_credentials(harness: Harness, mocker):
    """The s3-credentials relation produces the expected secret context.

    The endpoint scheme is stripped (host[:port] only) and translated into the
    s3-usehttps annotation, and the region is taken from the relation data.
    """
    mocker.patch("charm.S3Requirer")
    harness.begin()
    harness.add_relation("s3-credentials", "s3-integrator")
    harness.charm.s3_requirer.get_storage_connection_info.return_value = dict(S3_CONNECTION_INFO)

    context = harness.charm._get_storage_secrets_context()

    assert context == {
        "secret_name": "kserve-controller-s3",
        "s3_endpoint": "my-s3.example.com:9000",
        "s3_usehttps": "1",
        "s3_region": S3_CREDENTIALS_REGION,
        "s3_useanoncredential": "false",
        "s3_access_key": S3_CREDENTIALS_ACCESS_KEY,
        "s3_secret_access_key": S3_CREDENTIALS_SECRET_KEY,
    }


def test_get_storage_secrets_context_s3_http_endpoint_default_region(harness: Harness, mocker):
    """An http endpoint sets s3-usehttps to 0 and a missing region falls back to the default."""
    mocker.patch("charm.S3Requirer")
    harness.begin()
    harness.add_relation("s3-credentials", "s3-integrator")
    connection_info = dict(S3_CONNECTION_INFO)
    connection_info["endpoint"] = "http://minio:9000"
    del connection_info["region"]
    harness.charm.s3_requirer.get_storage_connection_info.return_value = connection_info

    context = harness.charm._get_storage_secrets_context()

    assert context["s3_endpoint"] == "minio:9000"
    assert context["s3_usehttps"] == "0"
    assert context["s3_region"] == "us-east-1"


def test_both_storage_relations_blocks_unit_status(
    harness: Harness, mocker, mocked_resource_handler
):
    """Relating both object-storage and s3-credentials sets the unit to BlockedStatus.

    Exercises the full event flow (not just the helper) to verify the ErrorWithStatus
    raised in _get_storage_secrets_context is translated into a BlockedStatus by _on_event.
    """
    mocker.patch("charm.S3Requirer")
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler

    # Make everything before send_object_storage_manifests() succeed:
    # standard mode + gateway-metadata relation -> would otherwise be Active.
    harness.update_config({"deployment-mode": "standard"})
    harness.add_relation("gateway-metadata", "istio-ingress-k8s")
    mocker.patch.object(
        harness.charm.gateway_info,
        "get_metadata",
        return_value=GatewayMetadata(
            namespace="test-namespace",
            deployment_name="test-deployment",
            gateway_name="test-gateway",
            service_account="test-service-account",
        ),
    )

    # Relate BOTH storage backends -> charm must block.
    harness.add_relation("object-storage", "minio")
    harness.add_relation("s3-credentials", "s3-integrator")

    harness.charm.on.install.emit()

    assert harness.charm.model.unit.status == BlockedStatus(
        "Too many object storage relations. Please relate to only one of "
        "`object-storage` or `s3-credentials`."
    )


def test_get_storage_secrets_context_no_relation(harness: Harness, mocker):
    """No storage relation yields no secret context."""
    harness.begin()
    mocker.patch.object(harness.charm, "_get_interfaces", return_value={})

    assert harness.charm._get_storage_secrets_context() is None


def test_get_storage_secrets_context_s3_incomplete_data_blocked(harness: Harness, mocker):
    """Incomplete s3-credentials relation data blocks the charm."""
    mocker.patch("charm.S3Requirer")
    harness.begin()
    harness.add_relation("s3-credentials", "s3-integrator")
    # endpoint is missing from the relation data
    harness.charm.s3_requirer.get_storage_connection_info.return_value = {
        "access-key": S3_CREDENTIALS_ACCESS_KEY,
        "secret-key": S3_CREDENTIALS_SECRET_KEY,
    }

    with pytest.raises(ErrorWithStatus) as exc_info:
        harness.charm._get_storage_secrets_context()

    assert isinstance(exc_info.value.status, BlockedStatus)


def test_get_storage_secrets_context_s3_no_data_waiting(harness: Harness, mocker):
    """An established s3-credentials relation with no data sets the charm to waiting."""
    mocker.patch("charm.S3Requirer")
    harness.begin()
    harness.add_relation("s3-credentials", "s3-integrator")
    harness.charm.s3_requirer.get_storage_connection_info.return_value = {}

    with pytest.raises(ErrorWithStatus) as exc_info:
        harness.charm._get_storage_secrets_context()

    assert isinstance(exc_info.value.status, WaitingStatus)


def test_send_object_storage_manifests_s3(harness: Harness, mocker):
    """The s3-credentials backend renders and sends both the secret and service account."""
    mocker.patch("charm.S3Requirer")
    harness.begin()
    harness.add_relation("s3-credentials", "s3-integrator")
    harness.charm.s3_requirer.get_storage_connection_info.return_value = dict(S3_CONNECTION_INFO)
    mocked_send_manifests = mocker.patch.object(harness.charm, "_send_manifests")

    harness.charm.send_object_storage_manifests()

    # One call for the secret, one for the service account
    assert mocked_send_manifests.call_count == 2


def test_send_object_storage_manifests_no_relation(harness: Harness, mocker):
    """No storage relation means no manifests are sent."""
    harness.begin()
    mocker.patch.object(harness.charm, "_get_interfaces", return_value={})
    mocked_send_manifests = mocker.patch.object(harness.charm, "_send_manifests")

    harness.charm.send_object_storage_manifests()

    mocked_send_manifests.assert_not_called()


def test_send_object_storage_manifests_clears_stale_manifests(harness: Harness, mocker):
    """No storage relation clears manifests on the established dispatcher relations.

    When no storage relation is present but the secrets/service-accounts relations exist,
    empty manifests are sent so resource-dispatcher removes the previously-created
    Secret/ServiceAccount from user namespaces.
    """
    harness.begin()
    harness.add_relation("secrets", "resource-dispatcher")
    harness.add_relation("service-accounts", "resource-dispatcher")
    mocked_secrets_send = mocker.patch.object(harness.charm.secrets_manifests_wrapper, "send_data")
    mocked_service_accounts_send = mocker.patch.object(
        harness.charm.service_accounts_manifests_wrapper, "send_data"
    )

    harness.charm.send_object_storage_manifests()

    mocked_secrets_send.assert_called_once_with([])
    mocked_service_accounts_send.assert_called_once_with([])


@patch("charm.KServeControllerCharm._restart_controller_service")
def test_generate_gateways_context_standard_mode(
    _mocked_restart_controller_service,
    harness: Harness,
    mocker,
    mocked_resource_handler,
):
    """Assert the gateway context is correct in Standard mode."""
    harness.begin()

    # Change deployment-mode to Standard
    harness.update_config({"deployment-mode": "Standard"})

    # Add relation with ingress-gateway providers
    harness.add_relation("service-mesh", "istio-beacon-k8s")
    harness.add_relation("gateway-metadata", "istio-ingress-k8s")

    # mock the self.gateway_info.get_metadata() to return hardcoded valuesu
    mocker.patch.object(
        harness.charm.gateway_info,
        "get_metadata",
        return_value=(
            GatewayMetadata(
                namespace="test-namespace",
                deployment_name="test-deployment",
                gateway_name="test-gateway",
                service_account="test-service-account",
            )
        ),
    )

    gateway_context = harness.charm._generate_gateways_context()
    assert gateway_context["ingress_gateway_name"] == "test-gateway"
    assert gateway_context["ingress_gateway_namespace"] == "test-namespace"
    assert gateway_context["ingress_gateway_service_name"] == "test-deployment"


@pytest.mark.parametrize(
    "deployment_mode, expected_status",
    [
        (
            "Standard",
            ActiveStatus(),
        ),
        (
            "Knative",
            ActiveStatus(),
        ),
        (
            "RawDeployment",
            ActiveStatus(),
        ),
        (
            "Serverless",
            ActiveStatus(),
        ),
        (
            "NotAllowed",
            BlockedStatus("Please set deployment-mode to either Knative or Standard"),
        ),
    ],
)
def test_deployment_modes_values(
    mocked_resource_handler,
    mocker,
    harness: Harness,
    deployment_mode: str,
    expected_status: StatusBase,
):
    harness.begin()
    harness.charm._k8s_resource_handler = mocked_resource_handler

    relation_id_ingress = harness.add_relation("ingress-gateway", "istio-pilot")
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
    harness.update_relation_data(relation_id_ingress, "istio-pilot", remote_ingress_data)
    harness.update_relation_data(relation_id_local, "test-knative-serving", remote_local_data)

    harness.update_config({"deployment-mode": deployment_mode})

    assert harness.charm.model.unit.status == expected_status
