#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

    https://discourse.charmhub.io/t/4208
"""

import json
import logging
from base64 import b64encode
from pathlib import Path
from typing import Dict

import yaml
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus, GenericCharmRuntimeError
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from charmed_kubeflow_chisme.lightkube.batch import delete_many
from charmed_kubeflow_chisme.pebble import update_layer
from charms.istio_pilot.v0.istio_gateway_info import (
    GatewayRelationDataMissingError,
    GatewayRelationMissingError,
    GatewayRequirer,
)
from charms.resource_dispatcher.v0.kubernetes_manifests import (
    KubernetesManifest,
    KubernetesManifestRequirerWrapper,
)
from jinja2 import Template
from jsonschema import ValidationError
from lightkube import ApiError
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    Container,
    MaintenanceStatus,
    ModelError,
    WaitingStatus,
)
from ops.pebble import APIError, Layer, PathError, ProtocolError
from serialized_data_interface import (
    NoCompatibleVersions,
    NoVersionsListed,
    SerializedDataInterface,
    get_interfaces,
)
from serialized_data_interface.errors import RelationDataError

from certs import gen_certs

# from lightkube_custom_resources.serving import ClusterServingRuntime_v1alpha1

log = logging.getLogger(__name__)

CONFIG_FILES = ["src/templates/configmap_manifests.yaml.j2"]
CONTAINER_CERTS_DEST = "/tmp/k8s-webhook-server/serving-certs/"
DEFAULT_IMAGES_FILE = "src/default-custom-images.json"
with open(DEFAULT_IMAGES_FILE, "r") as json_file:
    DEFAULT_IMAGES = json.load(json_file)

K8S_RESOURCE_FILES = [
    "src/templates/crd_manifests.yaml.j2",
    "src/templates/auth_manifests.yaml.j2",
    "src/templates/serving_runtimes_manifests.yaml.j2",
    "src/templates/webhook_manifests.yaml.j2",
]

# Values for MinIO manifests https://kserve.github.io/website/0.11/modelserving/storage/s3/s3/
S3_USEANONCREDENTIALS = "false"
S3_REGION = "us-east-1"
S3_USEHTTPS = "0"

SECRETS_FILES = [
    "src/secrets/kserve-mlflow-minio-secret.yaml.j2",
]
SERVICE_ACCOUNTS_FILES = [
    "src/service-accounts/kserve-mlflow-minio-svc-account.yaml.j2",
]
NO_MINIO_RELATION_DATA = {}


def parse_images_config(config: str) -> Dict:
    """
    Parse a YAML config-defined images list.

    This function takes a YAML-formatted string 'config' containing a list of images
    and returns a dictionaryrepresenting the images.

    Args:
        config (str): YAML-formatted string representing a list of images.

    Returns:
        Dict: A list of images.
    """
    error_message = (
        f"Cannot parse a config-defined images list from config '{config}' - this"
        "config input will be ignored."
    )
    if not config:
        return []
    try:
        images = yaml.safe_load(config)
    except yaml.YAMLError as err:
        log.warning(f"{error_message}  Got error: {err}, while parsing the custom_image config.")
        raise ErrorWithStatus(error_message, BlockedStatus)
    return images


class KServeControllerCharm(CharmBase):
    """Charm the service."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.custom_images = []
        self.images_context = {}
        self._ingress_gateway_requirer = GatewayRequirer(self, relation_name="ingress-gateway")
        self._local_gateway_requirer = GatewayRequirer(self, relation_name="local-gateway")

        self.framework.observe(self.on.remove, self._on_remove)

        for event in [
            self.on.install,
            self.on.config_changed,
            self.on.kserve_controller_pebble_ready,
            self.on.kube_rbac_proxy_pebble_ready,
            self.on["local-gateway"].relation_changed,
            self.on["ingress-gateway"].relation_changed,
            self.on["object-storage"].relation_changed,
            self.on["secrets"].relation_changed,
            self.on["service-accounts"].relation_changed,
            self.on["ingress-gateway"].relation_broken,
            self.on["local-gateway"].relation_broken,
        ]:
            self.framework.observe(event, self._on_event)

        self._k8s_resource_handler = None
        self._crd_resource_handler = None
        self._cm_resource_handler = None
        self._secrets_manifests_wrapper = None
        self._service_accounts_manifests_wrapper = None
        self._lightkube_field_manager = "lightkube"
        self._controller_container_name = "kserve-controller"
        self.controller_container = self.unit.get_container(self._controller_container_name)
        self._controller_service_name = self.app.name
        self._namespace = self.model.name
        self._webhook_service_name = "kserve-webhook-server-service"

        # Generate self-signed certificates and store them
        self._gen_certs_if_missing()

        self._rbac_proxy_container_name = "kube-rbac-proxy"
        self.rbac_proxy_container = self.unit.get_container(self._rbac_proxy_container_name)

    @property
    def _context(self):
        """Returns a dictionary containing context to be used for rendering."""
        ca_context = b64encode(self._stored.ca.encode("ascii"))
        return {
            "app_name": self.app.name,
            "namespace": self.model.name,
            "cert": f"'{ca_context.decode('utf-8')}'",
        }

    @property
    def _inference_service_context(self):
        """Context for rendering the inferenceservive-config ConfigMap."""
        # Ensure any input is valid for deployment mode
        deployment_mode = self.model.config["deployment-mode"].lower()
        if deployment_mode == "serverless":
            deployment_mode = "Serverless"
        elif deployment_mode == "rawdeployment":
            deployment_mode = "RawDeployment"
        else:
            raise ErrorWithStatus(
                "Please set deployment-mode to either Serverless or RawDeployment", BlockedStatus
            )

        inference_service_context = {
            "ingress_domain": self.model.config["domain-name"],
            "deployment_mode": deployment_mode,
            "namespace": self.model.name,
        }
        # Generate and add gateway context
        gateways_context = self._generate_gateways_context()
        inference_service_context.update(gateways_context)
        return inference_service_context

    @property
    def k8s_resource_handler(self):
        """Returns an instance of the KubernetesResourceHandler."""
        if not self._k8s_resource_handler:
            self._k8s_resource_handler = KubernetesResourceHandler(
                field_manager=self._lightkube_field_manager,
                template_files=K8S_RESOURCE_FILES,
                context={**self._context, **self.images_context},
                logger=log,
            )
        return self._k8s_resource_handler

    @property
    def cm_resource_handler(self):
        """Returns an instance of the KubernetesResourceHandler."""
        if not self._cm_resource_handler:
            self._cm_resource_handler = KubernetesResourceHandler(
                field_manager=self._lightkube_field_manager,
                template_files=CONFIG_FILES,
                context={**self._inference_service_context, **self.images_context},
                logger=log,
            )
        return self._cm_resource_handler

    @property
    def _controller_pebble_layer(self):
        """Return the Pebble layer for the workload."""
        return Layer(
            {
                "services": {
                    self._controller_container_name: {
                        "override": "replace",
                        "summary": "KServe Controller",
                        "command": "/manager --metrics-addr=:8080",
                        "startup": "enabled",
                        "environment": {
                            "POD_NAMESPACE": self.model.name,
                            "SECRET_NAME": "kserve-webhook-server-cert",
                        },
                    },
                }
            }
        )

    @property
    def _rbac_proxy_pebble_layer(self):
        """Return the Pebble layer for the workload."""
        return Layer(
            {
                "services": {
                    self._rbac_proxy_container_name: {
                        "override": "replace",
                        "summary": "Kube Rbac Proxy",
                        "command": "/usr/local/bin/kube-rbac-proxy --secure-listen-address=0.0.0.0:8443 --upstream=http://127.0.0.1:8080 --logtostderr=true --v=10",  # noqa E501
                        "startup": "enabled",
                    }
                }
            }
        )

    @property
    def _ingress_gateway_info(self):
        """Returns the ingress gateway info."""
        return self._ingress_gateway_requirer.get_relation_data()

    @property
    def _local_gateway_info(self):
        """Returns the local gateway info."""
        return self._local_gateway_requirer.get_relation_data()

    @property
    def secrets_manifests_wrapper(self):
        if not self._secrets_manifests_wrapper:
            self._secrets_manifests_wrapper = KubernetesManifestRequirerWrapper(
                charm=self, relation_name="secrets"
            )
        return self._secrets_manifests_wrapper

    @property
    def service_accounts_manifests_wrapper(self):
        if not self._service_accounts_manifests_wrapper:
            self._service_accounts_manifests_wrapper = KubernetesManifestRequirerWrapper(
                charm=self, relation_name="service-accounts"
            )
        return self._service_accounts_manifests_wrapper

    def _get_interfaces(self):
        # Remove this abstraction when SDI adds .status attribute to NoVersionsListed,
        # NoCompatibleVersionsListed:
        # https://github.com/canonical/serialized-data-interface/issues/26
        try:
            interfaces = get_interfaces(self)
        except NoVersionsListed as err:
            raise ErrorWithStatus((err), WaitingStatus)
        except NoCompatibleVersions as err:
            raise ErrorWithStatus(str(err), BlockedStatus)
        except RelationDataError as err:
            raise ErrorWithStatus(str(err), BlockedStatus)
        return interfaces

    def _validate_sdi_interface(self, interfaces: dict, relation_name: str, default_return=None):
        """Validates data received from SerializedDataInterface, returning the data if valid.

        Optionally can return a default_return value when no relation is established

        Raises:
            ErrorWithStatus(..., Blocked) when no relation established (unless default_return set)
            ErrorWithStatus(..., Blocked) if interface is not using SDI
            ErrorWithStatus(..., Blocked) if data in interface fails schema check
            ErrorWithStatus(..., Waiting) if we have a relation established but no data passed

        Params:
            interfaces:

        Returns:
              (dict) interface data
        """
        # If nothing is related to this relation, return a default value or raise an error
        if relation_name not in interfaces or interfaces[relation_name] is None:
            return default_return

        relations = interfaces[relation_name]
        if not isinstance(relations, SerializedDataInterface):
            raise ErrorWithStatus(
                f"Unexpected error with {relation_name} relation data - data not as expected",
                BlockedStatus,
            )

        # Get and validate data from the relation
        try:
            # relations is a dict of {(ops.model.Relation, ops.model.Application): data}
            unpacked_relation_data = relations.get_data()
        except ValidationError as val_error:
            # Validation in .get_data() ensures if data is populated, it matches the schema and is
            # not incomplete
            self.logger.error(val_error)
            raise ErrorWithStatus(
                f"Found incomplete/incorrect relation data for {relation_name}. See logs",
                BlockedStatus,
            )

        # Check if we have an established relation with no data exchanged
        if len(unpacked_relation_data) == 0:
            raise ErrorWithStatus(f"Waiting for {relation_name} relation data", WaitingStatus)

        # Unpack data (we care only about the first element)
        data_dict = list(unpacked_relation_data.values())[0]

        # Catch if empty data dict is received (JSONSchema ValidationError above does not raise
        # when this happens)
        # Remove once addressed in:
        # https://github.com/canonical/serialized-data-interface/issues/28
        if len(data_dict) == 0:
            raise ErrorWithStatus(
                f"Found empty relation data for {relation_name}",
                BlockedStatus,
            )

        return data_dict

    def _get_object_storage(self, interfaces, default_return):
        """Retrieve object-storage relation data."""
        relation_name = "object-storage"
        return self._validate_sdi_interface(interfaces, relation_name, default_return)

    def _create_manifests(self, manifest_files, context):
        """Create manifests string for given folder and context."""
        manifests = []
        for file in manifest_files:
            template = Template(Path(file).read_text())
            rendered_template = template.render(**context)
            manifest = KubernetesManifest(rendered_template)
            manifests.append(manifest)
        return manifests

    def get_images(
        self, default_images: Dict[str, str], custom_images: Dict[str, str]
    ) -> Dict[str, str]:
        """
        Combine default images with custom images.

        This function takes two dictionaries, 'default_images' and 'custom_images',
        representing the default set of images and the custom set of images respectively.
        It combines the custom images into the default image list, overriding any matching
        image names from the default list with the custom ones.

        Args:
            default_images (Dict[str, str]): A dictionary containing the default image names
                as keys and their corresponding default image URIs as values.
            custom_images (Dict[str, str]): A dictionary containing the custom image names
                as keys and their corresponding custom image URIs as values.

        Returns:
            Dict[str, str]: A dictionary representing the combined images, where image names
            from the custom_images override any matching image names from the default_images.
        """
        images = default_images
        for image_name, custom_image in custom_images.items():
            if custom_image:
                if image_name in images:
                    images[image_name] = custom_image
                else:
                    log.warning(f"image_name {image_name} not in image list, ignoring.")

        # This are special cases comfigmap where they need to be split into image and version
        for image_name in [
            "configmap__explainers__alibi",
            "configmap__explainers__art",
        ]:
            images[f"{image_name}__image"], images[f"{image_name}__version"] = images[
                image_name
            ].rsplit(":", 1)
        return images

    def send_object_storage_manifests(self):
        """Send object storage related manifests in case the object storage relation exists"""
        interfaces = self._get_interfaces()
        object_storage_data = self._get_object_storage(interfaces, NO_MINIO_RELATION_DATA)

        # Relation is not present
        if object_storage_data == NO_MINIO_RELATION_DATA:
            return

        secrets_context = {
            "secret_name": f"{self.app.name}-s3",
            "s3_endpoint": f"{object_storage_data['service']}.{object_storage_data['namespace']}:{object_storage_data['port']}",  # noqa: E501
            "s3_usehttps": S3_USEHTTPS,
            "s3_region": S3_REGION,
            "s3_useanoncredential": S3_USEANONCREDENTIALS,
            "s3_access_key": object_storage_data["access-key"],
            "s3_secret_access_key": object_storage_data["secret-key"],
        }

        service_accounts_context = {
            "svc_account_name": f"{self.app.name}-s3",
            "secret_name": f"{self.app.name}-s3",
        }

        secrets_manifests = self._create_manifests(SECRETS_FILES, secrets_context)
        service_accounts_manifests = self._create_manifests(
            SERVICE_ACCOUNTS_FILES, service_accounts_context
        )

        self.secrets_manifests_wrapper.send_data(secrets_manifests)
        self.service_accounts_manifests_wrapper.send_data(service_accounts_manifests)

    def _on_event(self, event):
        try:
            self.custom_images = parse_images_config(self.model.config["custom_images"])
            self.images_context = self.get_images(DEFAULT_IMAGES, self.custom_images)
            self.unit.status = MaintenanceStatus("Creating k8s resources")
            self.k8s_resource_handler.apply()
            self.cm_resource_handler.apply()
            self.send_object_storage_manifests()
            self._upload_certs_to_container(
                container=self.controller_container,
                destination_path=CONTAINER_CERTS_DEST,
                certs_store=self._stored,
            )
            # update kserve-controller layer
            update_layer(
                self._controller_container_name,
                self.controller_container,
                self._controller_pebble_layer,
                log,
            )
            # update kube-rbac-proxy layer
            update_layer(
                self._rbac_proxy_container_name,
                self.rbac_proxy_container,
                self._rbac_proxy_pebble_layer,
                log,
            )

            # The kserve-controller service must be restarted whenever the
            # configuration is changed, otherwise the service will remain
            # unaware of such changes.
            self._restart_controller_service()
        except ErrorWithStatus as err:
            self.model.unit.status = err.status
            log.error(f"Failed to handle {event} with error: {err}")
            return
        except ApiError as api_err:
            log.error(api_err)
            raise
        self.model.unit.status = ActiveStatus()

    def _on_remove(self, event):
        try:
            self.custom_images = parse_images_config(self.model.config["custom_images"])
            self.images_context = self.get_images(DEFAULT_IMAGES, self.custom_images)
        except ErrorWithStatus as err:
            self.model.unit.status = err.status
            log.error(f"Failed to handle {event} with error: {err}")
            return
        self.unit.status = MaintenanceStatus("Removing k8s resources")
        k8s_resources_manifests = self.k8s_resource_handler.render_manifests()
        cm_resources_manifests = self.cm_resource_handler.render_manifests()
        try:
            delete_many(
                self.k8s_resource_handler.lightkube_client,
                k8s_resources_manifests,
            )
            delete_many(
                self.cm_resource_handler.lightkube_client,
                cm_resources_manifests,
            )

        except ApiError as e:
            log.warning(f"Failed to delete resources, with error: {e}")
            raise e
        self.unit.status = MaintenanceStatus("K8s resources removed")

    def _check_container_connection(self, container: Container) -> None:
        """Check if connection can be made with container.

        Args:
            container: the named container in a unit to check.

        Raises:
            ErrorWithStatus if the connection cannot be made.
        """
        if not container.can_connect():
            raise ErrorWithStatus("Pod startup is not complete", MaintenanceStatus)

    def _generate_gateways_context(self) -> dict:
        """Generates the ingress context based on certain rules.

        Returns:
            gateways_context (dict): a dictionary of all the fields in the ingress
                section of the inferenceservice-config
        Raises:
            GatewayRelationMissingError: if any of the required relations are missing
            GatewayRelationDataMissingError: if relation data is missing or incomplete
        """
        # Get the ingress-gateway info. This should always be known by this charm.
        try:
            ingress_gateway_info = self._ingress_gateway_info
        except GatewayRelationMissingError:
            raise ErrorWithStatus("Please relate to istio-pilot:gateway-info", BlockedStatus)
        except GatewayRelationDataMissingError:
            log.error("Missing or incomplete ingress gateway data.")
            raise ErrorWithStatus("Waiting for ingress gateway data.", WaitingStatus)

        # A temporal context with values only from ingress gateway
        # FIXME: the ingress_gateway_service_name is hardcoded in istio-pilot
        # and that information is not shared through the relation
        gateways_context = {
            "ingress_gateway_name": ingress_gateway_info["gateway_name"],
            "ingress_gateway_namespace": ingress_gateway_info["gateway_namespace"],
            "ingress_gateway_service_name": "istio-ingressgateway-workload",
            "local_gateway_name": "",
            "local_gateway_namespace": "",
            "local_gateway_service_name": "",
        }

        # Get the local-gateway info. This value should only
        # be get and rendered in Serverless Mode.
        if self.model.config["deployment-mode"].lower() == "serverless":
            try:
                local_gateway_info = self._local_gateway_info
                # FIXME: the local_gateway_service_name is hardcoded in knative-serving
                # and that information is not shared through the relation
                gateways_context.update(
                    {
                        "local_gateway_name": local_gateway_info["gateway_name"],
                        "local_gateway_namespace": local_gateway_info["gateway_namespace"],
                        "local_gateway_service_name": "knative-local-gateway",
                    }
                )
            except GatewayRelationMissingError:
                raise ErrorWithStatus(
                    "Please relate to knative-serving:local-gateway", BlockedStatus
                )
            except GatewayRelationDataMissingError:
                log.error("Missing or incomplete local gateway data.")
                raise ErrorWithStatus("Waiting for local gateway data.", WaitingStatus)

        return gateways_context

    def _gen_certs_if_missing(self) -> None:
        """Generate certificates if they don't already exist in _stored."""
        log.info("Generating certificates if missing.")
        cert_attributes = ["cert", "ca", "key"]
        # Generate new certs if any cert attribute is missing
        for cert_attribute in cert_attributes:
            try:
                getattr(self._stored, cert_attribute)
                log.info(f"Certificate {cert_attribute} already exists, skipping generation.")
            except AttributeError:
                self._gen_certs()
                return

    def _gen_certs(self):
        """Refresh the certificates, overwriting all attributes if any attribute is missing."""
        log.info("Generating certificates..")
        certs = gen_certs(
            service_name=self._controller_service_name,
            namespace=self._namespace,
            webhook_service=self._webhook_service_name,
        )
        for k, v in certs.items():
            setattr(self._stored, k, v)

    def _upload_certs_to_container(
        self, container: Container, destination_path: str, certs_store: StoredState
    ) -> None:
        """Upload generated certs to container.

        Args:
            container (Container): the container object to push certs to.
            destination_path (str): path in str format where certificates will
                be stored in the container.
            certs_store (StoredState): an object where the certificate contents are stored.
        """
        try:
            self._check_container_connection(container)
        except ErrorWithStatus as error:
            self.model.unit.status = error.status
            return

        try:
            container.push(f"{destination_path}/tls.key", certs_store.key, make_dirs=True)
            container.push(f"{destination_path}/tls.crt", certs_store.cert, make_dirs=True)
            container.push(f"{destination_path}/ca.crt", certs_store.ca, make_dirs=True)
        except (ProtocolError, PathError) as e:
            raise GenericCharmRuntimeError("Failed to push certs to container") from e

    def _restart_controller_service(self) -> None:
        """Restart the kserve-controller service.

        This helper allows restarting the kserve-controller service
        from any state (running, not running).
        Since this helper is not responsible for setting up the service,
        it returns if the kserve-controller container is not reachable
        or the kserve-controller service is not found.
        """
        # Check for container connection before attempting to restart the service
        if not self.controller_container.can_connect():
            log.info("Skipping the service restart, kserve-controller container is not reachable")
            return

        # If the kserve-controller service is not running, do nothing
        try:
            self.controller_container.get_service(self._controller_container_name).is_running()
        except ModelError:
            log.info("Service not found, nothing to restart.")
            return

        try:
            self.controller_container.restart(self._controller_container_name)
        except APIError as err:
            raise GenericCharmRuntimeError(
                f"Failed to restart {self._controller_container_name} service"
            ) from err


if __name__ == "__main__":
    main(KServeControllerCharm)
