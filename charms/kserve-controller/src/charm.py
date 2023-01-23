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
import traceback

from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from charmed_kubeflow_chisme.lightkube.batch import delete_many
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus
from charmed_kubeflow_chisme.pebble import update_layer
from lightkube.generic_resource import load_in_cluster_generic_resources
from lightkube import ApiError
from ops.charm import CharmBase
from ops.main import main
from subprocess import check_call
from pathlib import Path
from base64 import b64encode

from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from ops.pebble import ChangeError, Layer

#from lightkube_custom_resources.serving import ClusterServingRuntime_v1alpha1

log = logging.getLogger(__name__)

K8S_RESOURCE_FILES = [
    "src/templates/crd_manifests.yaml.j2",
    "src/templates/auth_manifests.yaml.j2",
    "src/templates/serving_runtimes_manifests.yaml.j2",
    "src/templates/webhook_manifests.yaml.j2",
    "src/templates/configmap_manifests.yaml.j2",
]
CONFIG_FILE = "src/config/inference-config.j2"


class KServeControllerCharm(CharmBase):
    """Charm the service."""


    def __init__(self, *args):
        super().__init__(*args)

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.remove, self._on_remove)
        self.framework.observe(
            self.on.kserve_controller_pebble_ready, self._on_kserve_controller_ready
        )
        self.framework.observe(
            self.on.kube_rbac_proxy_pebble_ready, self._on_kube_rbac_proxy_ready
        )

        self._k8s_resource_handler = None
        self._crd_resource_handler = None
        self._lightkube_field_manager = "lightkube"
        self._controller_container_name = "kserve-controller"
        self.controller_container = self.unit.get_container(self._controller_container_name)

        self._rbac_proxy_container_name = "kube-rbac-proxy"
        self.rbac_proxy_container = self.unit.get_container(self._rbac_proxy_container_name)

    @property
    def _context(self):
        """Returns a dictionary containing context to be used for rendering."""
        self.gen_certs(self.model.name, self.app.name)
        ca_context = b64encode(Path('/run/ca.crt').read_text().encode("ascii"))
        return {
            "app_name": self.app.name,
            "namespace": self.model.name,
            "cert": f"'{ca_context.decode('utf-8')}'",
        }

    @property
    def k8s_resource_handler(self):
        if not self._k8s_resource_handler:
            self._k8s_resource_handler = KubernetesResourceHandler(
                field_manager=self._lightkube_field_manager,
                template_files=K8S_RESOURCE_FILES,
                context=self._context,
                logger=log,
            )
        return self._k8s_resource_handler

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

    def _upload_certs_to_container(self):
        """Upload generated certificates to workload container."""
        if not self.controller_container.can_connect():
            raise ErrorWithStatus("Failed to upload files, container is not ready", WaitingStatus)

        # Generate certificates
        self.gen_certs(self.model.name, self.app.name)

        # Push certificate files to workload container
        self.controller_container.push("/tmp/k8s-webhook-server/serving-certs/tls.crt", Path("/run/cert.pem").read_text(), make_dirs=True)
        self.controller_container.push("/tmp/k8s-webhook-server/serving-certs/tls.key", Path("/run/server.key").read_text(), make_dirs=True)

    def _on_kserve_controller_ready(self, event):
        """Define and start a workload using the Pebble API.

        Learn more about Pebble layers at https://github.com/canonical/pebble
        """
        try:

            self._upload_certs_to_container()
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


        #TODO determine status checking if controller is also up
        self.unit.status = ActiveStatus()

    def _on_install(self, event):
        try:
            self.unit.status = MaintenanceStatus("Creating k8s resources")
            self.k8s_resource_handler.apply()
        except ApiError as e:
            log.error(e)
            raise
        self.model.unit.status = ActiveStatus()

    def _on_remove(self, _):
        self.unit.status = MaintenanceStatus("Removing k8s resources")
        k8s_resources_manifests = self.k8s_resource_handler.render_manifests()
        try:
            delete_many(
                    self.k8s_resource_handler.lightkube_client,
                    k8s_resources_manifests,
                )
        except ApiError as e:
            log.warning(f"Failed to delete resources, with error: {e}")
            raise e
        self.unit.status = MaintenanceStatus("K8s resources removed")

    def gen_certs(self, namespace, service_name):
        if Path("/run/cert.pem").exists():
            log.info("Found existing cert.pem, not generating new cert.")
            return
    
        Path("/run/ssl.conf").write_text(ssl_conf)

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


if __name__ == "__main__":
    main(KServeControllerCharm)
