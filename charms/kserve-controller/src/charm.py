#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

    https://discourse.charmhub.io/t/4208
"""

import logging
from base64 import b64encode
from pathlib import Path
from subprocess import check_call

from charmed_kubeflow_chisme.exceptions import ErrorWithStatus, GenericCharmRuntimeError
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from charmed_kubeflow_chisme.lightkube.batch import delete_many
from charmed_kubeflow_chisme.pebble import update_layer
from charms.istio_pilot.v0.istio_gateway_info import (
    GatewayRelationDataMissingError,
    GatewayRelationMissingError,
    GatewayRequirer,
)
from lightkube import ApiError
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from ops.pebble import APIError, Layer

# from lightkube_custom_resources.serving import ClusterServingRuntime_v1alpha1

log = logging.getLogger(__name__)

K8S_RESOURCE_FILES = [
    "src/templates/crd_manifests.yaml.j2",
    "src/templates/auth_manifests.yaml.j2",
    "src/templates/serving_runtimes_manifests.yaml.j2",
    "src/templates/webhook_manifests.yaml.j2",
]
CONFIG_FILES = ["src/templates/configmap_manifests.yaml.j2"]


class KServeControllerCharm(CharmBase):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(*args)
        self._ingress_gateway_requirer = GatewayRequirer(self, relation_name="ingress-gateway")
        self._local_gateway_requirer = GatewayRequirer(self, relation_name="local-gateway")

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.remove, self._on_remove)
        self.framework.observe(
            self.on.kserve_controller_pebble_ready, self._on_kserve_controller_ready
        )
        self.framework.observe(
            self.on.kube_rbac_proxy_pebble_ready, self._on_kube_rbac_proxy_ready
        )
        self.framework.observe(
            self.on["ingress-gateway"].relation_changed, self._on_ingress_gateway_relation_changed
        )
        # Observe if relation is removed by juju remove-application or juju remove-relation
        self.framework.observe(
            self.on["ingress-gateway"].relation_broken, self._on_ingress_gateway_relation_broken
        )
        self.framework.observe(
            self.on["local-gateway"].relation_changed, self._on_local_gateway_relation_changed
        )
        self.framework.observe(
            self.on["local-gateway"].relation_broken, self._on_local_gateway_relation_broken
        )

        self._k8s_resource_handler = None
        self._crd_resource_handler = None
        self._cm_resource_handler = None
        self._lightkube_field_manager = "lightkube"
        self._controller_container_name = "kserve-controller"
        self.controller_container = self.unit.get_container(self._controller_container_name)

        self._rbac_proxy_container_name = "kube-rbac-proxy"
        self.rbac_proxy_container = self.unit.get_container(self._rbac_proxy_container_name)

    @property
    def _context(self):
        """Returns a dictionary containing context to be used for rendering."""
        self.gen_certs(self.model.name, self.app.name)
        ca_context = b64encode(Path("/run/ca.crt").read_text().encode("ascii"))
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
                context=self._context,
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
                context=self._inference_service_context,
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
                        "command": "/usr/local/bin/kube-rbac-proxy --secure-listen-address=0.0.0.0:8443 --upstream=http://127.0.0.1:8080 --logtostderr=true --v=10",
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

    def _on_kserve_controller_ready(self, event):
        """Define and start a workload using the Pebble API.

        Learn more about Pebble layers at https://github.com/canonical/pebble
        """
        try:
            self.gen_certs(self.model.name, self.app.name)

            self.controller_container.push(
                "/tmp/k8s-webhook-server/serving-certs/tls.crt",
                Path("/run/cert.pem").read_text(),
                make_dirs=True,
            )
            self.controller_container.push(
                "/tmp/k8s-webhook-server/serving-certs/tls.key",
                Path("/run/server.key").read_text(),
                make_dirs=True,
            )

            update_layer(
                self._controller_container_name,
                self.controller_container,
                self._controller_pebble_layer,
                log,
            )
        except ErrorWithStatus as e:
            self.model.unit.status = e.status
            if isinstance(e.status, BlockedStatus):
                log.error(str(e.msg))
            else:
                log.info(str(e.msg))

        # TODO determine status checking if rbac proxy is also up
        self.unit.status = ActiveStatus()

    def _on_kube_rbac_proxy_ready(self, event):
        """Define and start a workload using the Pebble API.

        Learn more about Pebble layers at https://github.com/canonical/pebble
        """
        try:
            update_layer(
                self._rbac_proxy_container_name,
                self.rbac_proxy_container,
                self._rbac_proxy_pebble_layer,
                log,
            )
        except ErrorWithStatus as e:
            self.model.unit.status = e.status
            if isinstance(e.status, BlockedStatus):
                log.error(str(e.msg))
            else:
                log.info(str(e.msg))

        # TODO determine status checking if controller is also up
        self.unit.status = ActiveStatus()

    def _on_install(self, event):
        try:
            self.unit.status = MaintenanceStatus("Creating k8s resources")
            self.k8s_resource_handler.apply()
            self.cm_resource_handler.apply()
        except ErrorWithStatus as err:
            self.model.unit.status = err.status
            log.error(f"Failed to handle {event} with error: {err}")
            return
        except ApiError as api_err:
            log.error(api_err)
            raise
        else:
            self.model.unit.status = ActiveStatus()

    def _on_config_changed(self, event):
        self._on_install(event)

        # The kserve-controller service must be restarted whenever the
        # configuration is changed, otherwise the service will remain
        # unaware of the changes.
        self._restart_controller_service()

    def _on_remove(self, _):
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

    def _on_ingress_gateway_relation_changed(self, event) -> None:
        """Handle the ingress-gateway relation changed event."""
        # Just call the event handler that applies manifest files
        self._on_install(event)

    def _on_local_gateway_relation_changed(self, event) -> None:
        """Handle the local-gateway relation changed event."""
        # Just call the event handler that applies manifest files
        self._on_install(event)

    def _on_ingress_gateway_relation_broken(self, _) -> None:
        """Handle the ingress-gateway relation broken event."""
        # Ingress is always needed, so immediately go into BlockedStatus
        self.unit.status = BlockedStatus("Please relate to istio-pilot:gateway-info")
        return

    def _on_local_gateway_relation_broken(self, _) -> None:
        """Handle the local-gateway relation broken event."""
        if self.model.config["deployment-mode"].lower() == "serverless":
            self.unit.status = BlockedStatus("Please relate to knative-serving:local-gateway")
        return

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
                        "local_gateway_namespace": ingress_gateway_info["gateway_namespace"],
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

    def gen_certs(self, namespace, service_name):
        """Generate certificates."""
        if Path("/run/cert.pem").exists():
            log.info("Found existing cert.pem, not generating new cert.")
            return

        Path("/run/ssl.conf").write_text(
            f"""[ req ]
default_bits = 2048
prompt = no
default_md = sha256
req_extensions = req_ext
distinguished_name = dn
[ dn ]
C = GB
ST = Canonical
L = Canonical
O = Canonical
OU = Canonical
CN = 127.0.0.1
[ req_ext ]
subjectAltName = @alt_names
[ alt_names ]
DNS.1 = {service_name}
DNS.2 = {service_name}.{namespace}
DNS.3 = {service_name}.{namespace}.svc
DNS.4 = {service_name}.{namespace}.svc.cluster
DNS.5 = {service_name}.{namespace}.svc.cluster.local
DNS.6 = kserve-webhook-server-service
DNS.7 = kserve-webhook-server-service.{namespace}
DNS.8 = kserve-webhook-server-service.{namespace}.svc
DNS.9 = kserve-webhook-server-service.{namespace}.svc.cluster
DNS.10 = kserve-webhook-server-service.{namespace}.svc.cluster.local
IP.1 = 127.0.0.1
[ v3_ext ]
authorityKeyIdentifier=keyid,issuer:always
basicConstraints=CA:FALSE
keyUsage=keyEncipherment,dataEncipherment,digitalSignature
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=@alt_names"""
        )

        check_call(["openssl", "genrsa", "-out", "/run/ca.key", "2048"])
        check_call(["openssl", "genrsa", "-out", "/run/server.key", "2048"])
        check_call(
            [
                "openssl",
                "req",
                "-x509",
                "-new",
                "-sha256",
                "-nodes",
                "-days",
                "3650",
                "-key",
                "/run/ca.key",
                "-subj",
                "/CN=127.0.0.1",
                "-out",
                "/run/ca.crt",
            ]
        )
        check_call(
            [
                "openssl",
                "req",
                "-new",
                "-sha256",
                "-key",
                "/run/server.key",
                "-out",
                "/run/server.csr",
                "-config",
                "/run/ssl.conf",
            ]
        )
        check_call(
            [
                "openssl",
                "x509",
                "-req",
                "-sha256",
                "-in",
                "/run/server.csr",
                "-CA",
                "/run/ca.crt",
                "-CAkey",
                "/run/ca.key",
                "-CAcreateserial",
                "-out",
                "/run/cert.pem",
                "-days",
                "365",
                "-extensions",
                "v3_ext",
                "-extfile",
                "/run/ssl.conf",
            ]
        )

    def _restart_controller_service(self) -> None:
        """Restart the kserve-controller service."""
        try:
            self.controller_container.restart(self._controller_container_name)
        except APIError as err:
            raise GenericCharmRuntimeError(
                f"Failed to restart {self._controller_container_name} service"
            ) from err


if __name__ == "__main__":
    main(KServeControllerCharm)
