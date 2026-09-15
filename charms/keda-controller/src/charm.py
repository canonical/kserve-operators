#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""KEDA (Kubernetes Event-driven Autoscaling) operator charm.

Packages upstream KEDA as a single sidecar charm running three workload
containers in one pod - the operator, the external-metrics API server and the
admission webhooks - and applies the KEDA CRDs, RBAC, Services, the
``external.metrics.k8s.io`` APIService and the validating webhook configuration.

The charm manages the serving certificates itself (self-signed, pushed into the
containers over Pebble) and disables KEDA's built-in cert rotation, because a
Juju sidecar charm cannot mount the rotation-managed Secret into the workload
containers. One certificate covers all three TLS endpoints: the metrics-apiserver
Service, the webhook Service and the in-pod operator gRPC server (127.0.0.1).
"""

import logging
from base64 import b64encode

import tenacity
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus, GenericCharmRuntimeError
from charmed_kubeflow_chisme.kubernetes import (
    KubernetesResourceHandler,
    create_charm_default_labels,
)
from charmed_kubeflow_chisme.lightkube.batch import delete_many
from charmed_kubeflow_chisme.pebble import update_layer
from charms.loki_k8s.v1.loki_push_api import LogForwarder
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from lightkube import ApiError, Client
from lightkube.core.exceptions import LoadResourceError
from lightkube.generic_resource import load_in_cluster_generic_resources
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from ops import main
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.model import (
    ActiveStatus,
    Container,
    MaintenanceStatus,
)
from ops.pebble import Layer, PathError, ProtocolError

from certs import gen_certs

log = logging.getLogger(__name__)

# Static CRDs plus the templated cluster resources the charm manages.
BASE_RESOURCE_FILES = [
    "src/templates/crd_manifests.yaml.j2",
    "src/templates/auth_manifests.yaml.j2",
    "src/templates/service_manifests.yaml.j2",
    "src/templates/apiservice_manifests.yaml.j2",
    "src/templates/webhook_manifests.yaml.j2",
]

# Shared serving-cert directory for all three containers (matches KEDA's images).
CERTS_DEST = "/certs"

KEDA_SYNC_RELATION = "keda"
OPERATOR_CONTAINER = "keda-operator"
METRICS_APISERVER_CONTAINER = "keda-metrics-apiserver"
WEBHOOKS_CONTAINER = "keda-admission-webhooks"

# Operator/webhook health probes; metrics-apiserver serves health over HTTPS on 6443.
# All three run in one pod (shared netns), so each Prometheus /metrics and health
# endpoint needs a distinct port to avoid "address already in use".
OPERATOR_HEALTH_PORT = 8081
WEBHOOK_HEALTH_PORT = 8085
# Plain-HTTP Prometheus /metrics endpoints exposed by each KEDA component.
OPERATOR_METRICS_PORT = 8080
ADAPTER_METRICS_PORT = 8082
WEBHOOK_METRICS_PORT = 8086
METRICS_APISERVER_PORT = 6443
# In-pod gRPC metrics service the adapter connects to (127.0.0.1 is in the cert SANs).
METRICS_SERVICE_ADDR = "127.0.0.1:9666"

KRH_SCOPE_BASE = "keda-base"

# Bounds for waiting on async resource deletion (CRD, APIService, webhooks, RBAC).
KEDA_DELETION_TIMEOUT = 300
KEDA_DELETION_POLL_INTERVAL = 5


class ObjectStillExistsError(Exception):
    """Exception for when a K8s object exists, while it should have been removed."""

    def __init__(self, resource_name: str):
        self.resource_name = resource_name
        super().__init__(f"Resource still exists: {resource_name}")


class KedaCharm(CharmBase):
    """Charm packaging the KEDA operator, metrics-apiserver and webhooks."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self._base_resource_handler = None
        self._lightkube_field_manager = "lightkube"

        self._metrics_apiserver_service = f"{self.app.name}-metrics-apiserver"
        self._webhook_service = f"{self.app.name}-webhooks"

        self.operator_container = self.unit.get_container(OPERATOR_CONTAINER)
        self.metrics_apiserver_container = self.unit.get_container(METRICS_APISERVER_CONTAINER)
        self.webhooks_container = self.unit.get_container(WEBHOOKS_CONTAINER)

        self._gen_certs_if_missing()

        for event in [
            self.on.install,
            self.on.config_changed,
            self.on.keda_operator_pebble_ready,
            self.on.keda_metrics_apiserver_pebble_ready,
            self.on.keda_admission_webhooks_pebble_ready,
            self.on.leader_elected,
            self.on.update_status,
            self.on[KEDA_SYNC_RELATION].relation_joined,
            self.on[KEDA_SYNC_RELATION].relation_changed,
        ]:
            self.framework.observe(event, self._on_event)
        self.framework.observe(self.on.remove, self._on_remove)

        # Expose each KEDA component's plain-HTTP /metrics endpoint to Prometheus.
        self.unit.set_ports(OPERATOR_METRICS_PORT, ADAPTER_METRICS_PORT, WEBHOOK_METRICS_PORT)
        self.metrics_endpoint = MetricsEndpointProvider(
            self,
            jobs=[
                {
                    "job_name": "keda-operator",
                    "static_configs": [{"targets": [f"*:{OPERATOR_METRICS_PORT}"]}],
                },
                {
                    "job_name": "keda-metrics-apiserver",
                    "static_configs": [{"targets": [f"*:{ADAPTER_METRICS_PORT}"]}],
                },
                {
                    "job_name": "keda-admission-webhooks",
                    "static_configs": [{"targets": [f"*:{WEBHOOK_METRICS_PORT}"]}],
                },
            ],
        )

        # Forward all three containers' logs to Loki when related to COS.
        self._logging = LogForwarder(charm=self)

    @property
    def _context(self):
        """Render context for the base resource templates."""
        ca_context = b64encode(self._stored.ca.encode("ascii"))
        return {
            "app_name": self.app.name,
            "namespace": self.model.name,
            "cert": f"'{ca_context.decode('utf-8')}'",
            # Service metrics targetPorts must match the ports the containers bind
            # (reassigned to avoid collisions in the shared pod netns).
            "adapter_metrics_port": ADAPTER_METRICS_PORT,
            "webhook_metrics_port": WEBHOOK_METRICS_PORT,
        }

    @property
    def _log_level(self) -> str:
        return str(self.model.config.get("log-level", "info")).strip() or "info"

    @property
    def _watch_namespace(self) -> str:
        return str(self.model.config.get("watch-namespace", "")).strip()

    @property
    def base_resource_handler(self):
        """K8s handler for the KEDA CRDs, RBAC, Services, APIService and webhooks."""
        if not self._base_resource_handler:
            self._base_resource_handler = KubernetesResourceHandler(
                field_manager=self._lightkube_field_manager,
                template_files=BASE_RESOURCE_FILES,
                context=self._context,
                labels=create_charm_default_labels(
                    self.app.name, self.model.name, scope=KRH_SCOPE_BASE
                ),
                logger=log,
            )
        return self._base_resource_handler

    def _sync_handler_resource_types(self, handler: KubernetesResourceHandler):
        """Set handler.resource_types from its current rendered manifests."""
        load_in_cluster_generic_resources(handler.lightkube_client)
        try:
            manifests = list(handler.render_manifests())
        except LoadResourceError as e:
            log.warning(
                "Skipping resource type sync for handler due to unresolved resource: %s", e
            )
            return []
        if manifests:
            handler.resource_types = {type(resource) for resource in manifests}
        return manifests

    @property
    def _common_env(self) -> dict:
        return {
            "POD_NAMESPACE": self.model.name,
            "WATCH_NAMESPACE": self._watch_namespace,
        }

    def _service_layer(
        self, container: str, summary: str, command: str, check_name=None, health_port=None
    ) -> Layer:
        """Build a single-service Pebble layer, optionally with an HTTP readiness check."""
        service = {
            "override": "replace",
            "summary": summary,
            "command": command,
            "startup": "enabled",
            "environment": self._common_env,
        }
        layer: dict = {"services": {container: service}}
        if check_name and health_port:
            service["on-check-failure"] = {check_name: "restart"}
            layer["checks"] = {
                check_name: {
                    "override": "replace",
                    "level": "ready",
                    "http": {"url": f"http://localhost:{health_port}/readyz"},
                }
            }
        return Layer(layer)

    @property
    def _operator_pebble_layer(self) -> Layer:
        command = (
            "/keda --leader-elect "
            f"--zap-log-level={self._log_level} "
            "--zap-encoder=console --zap-time-encoding=rfc3339 "
            "--enable-cert-rotation=false"
        )
        return self._service_layer(
            OPERATOR_CONTAINER, "KEDA operator", command, "operator-ready", OPERATOR_HEALTH_PORT
        )

    @property
    def _metrics_apiserver_pebble_layer(self) -> Layer:
        command = (
            "/keda-adapter "
            f"--secure-port={METRICS_APISERVER_PORT} "
            "--logtostderr=true --stderrthreshold=ERROR --v=0 "
            f"--port={ADAPTER_METRICS_PORT} "
            f"--client-ca-file={CERTS_DEST}/ca.crt "
            f"--tls-cert-file={CERTS_DEST}/tls.crt "
            f"--tls-private-key-file={CERTS_DEST}/tls.key "
            f"--cert-dir={CERTS_DEST} "
            f"--metrics-service-address={METRICS_SERVICE_ADDR}"
        )
        # No Pebble check: the adapter serves health over HTTPS on 6443, not plain HTTP.
        return self._service_layer(
            METRICS_APISERVER_CONTAINER, "KEDA external-metrics API server", command
        )

    @property
    def _webhooks_pebble_layer(self) -> Layer:
        command = (
            "/keda-admission-webhooks "
            f"--zap-log-level={self._log_level} "
            "--zap-encoder=console --zap-time-encoding=rfc3339 "
            f"--health-probe-bind-address=:{WEBHOOK_HEALTH_PORT} "
            f"--metrics-bind-address=:{WEBHOOK_METRICS_PORT} "
            f"--cert-dir={CERTS_DEST}"
        )
        return self._service_layer(
            WEBHOOKS_CONTAINER,
            "KEDA admission webhooks",
            command,
            "webhooks-ready",
            WEBHOOK_HEALTH_PORT,
        )

    @property
    def _containers(self):
        return (
            (self.operator_container, self._operator_pebble_layer),
            (self.metrics_apiserver_container, self._metrics_apiserver_pebble_layer),
            (self.webhooks_container, self._webhooks_pebble_layer),
        )

    def _on_event(self, event):
        """Main reconcile loop for the KEDA charm."""
        try:
            self.unit.status = MaintenanceStatus("Creating k8s resources")

            self._sync_handler_resource_types(self.base_resource_handler)
            self.base_resource_handler.apply()

            for container, layer in self._containers:
                self._upload_certs_to_container(container, CERTS_DEST)
                update_layer(container.name, container, layer, log)

            self.unit.status = ActiveStatus()
            self._publish_keda_sync_data(ready=True)
        except ErrorWithStatus as err:
            self._publish_keda_sync_data(ready=False)
            self.model.unit.status = err.status
            log.error("Failed to handle %s with error: %s", event, err)
            return
        except ApiError:
            self._publish_keda_sync_data(ready=False)
            log.exception("Kubernetes API error during reconcile")
            raise

    @tenacity.retry(
        stop=tenacity.stop_after_delay(KEDA_DELETION_TIMEOUT),
        wait=tenacity.wait_fixed(KEDA_DELETION_POLL_INTERVAL),
        reraise=True,
    )
    def ensure_resource_is_deleted(
        self, client: Client, resource_kind, resource_name: str, namespace: str = None
    ):
        """Block until a resource no longer exists, retrying on each check."""
        try:
            client.get(resource_kind, name=resource_name, namespace=namespace)
            log.info('Resource "%s" exists, retrying...', resource_name)
            raise ObjectStillExistsError(resource_name)
        except ApiError as e:
            if e.status.code == 404:
                return
            raise

    def _ensure_all_deleted(self, client: Client, resources: list) -> None:
        """Wait for every resource to be deleted, aggregating any that linger."""
        stuck = []
        for resource in resources:
            try:
                self.ensure_resource_is_deleted(
                    client=client,
                    resource_kind=type(resource),
                    resource_name=resource.metadata.name,
                    namespace=resource.metadata.namespace,
                )
            except ObjectStillExistsError as e:
                stuck.append(e.resource_name)
        if stuck:
            raise ObjectStillExistsError(", ".join(stuck))

    def _delete_base_resources_and_wait(self, client: Client, base_manifests: list) -> None:
        """Delete base resources CRD-first and block until gone.

        The CRDs are deleted first so their removal cascades to any ScaledObjects
        and lets the operator clear finalizers. The remaining cluster resources -
        including the singleton ``external.metrics.k8s.io`` APIService - are then
        deleted; leaving the APIService behind would wedge the aggregation layer.
        """
        if not base_manifests:
            return
        crd_manifests = [r for r in base_manifests if isinstance(r, CustomResourceDefinition)]
        non_crd_manifests = [
            r for r in base_manifests if not isinstance(r, CustomResourceDefinition)
        ]

        if crd_manifests:
            delete_many(client, crd_manifests, ignore_missing=True, logger=log)
            self._ensure_all_deleted(client, crd_manifests)

        self.base_resource_handler.delete(ignore_missing=True)
        self._ensure_all_deleted(client, non_crd_manifests)

    def _on_remove(self, _):
        """Delete all resources rendered by the base handler."""
        self.unit.status = MaintenanceStatus("Removing k8s resources")
        base_manifests = self._sync_handler_resource_types(self.base_resource_handler)
        client = self.base_resource_handler.lightkube_client
        try:
            self._delete_base_resources_and_wait(client, base_manifests)
        except ApiError as e:
            if e.status.code != 404:
                log.warning("Failed to delete resources with error: %s", e)
                raise
        except ObjectStillExistsError as e:
            log.warning(
                "Failed to remove resource: %s. Manual cleanup might be required",
                e.resource_name,
            )
            raise
        self.unit.status = MaintenanceStatus("K8s resources removed")

    def _publish_keda_sync_data(self, ready: bool) -> None:
        """Publish readiness contract on every ``keda`` relation."""
        if not self.unit.is_leader():
            return
        for relation in self.model.relations.get(KEDA_SYNC_RELATION, []):
            relation.data[self.app].update(
                {"ready": str(ready).lower(), "namespace": self.model.name}
            )

    def _gen_certs_if_missing(self) -> None:
        """Generate certs if they are not already present in stored state."""
        for cert_attribute in ["cert", "ca", "key"]:
            try:
                getattr(self._stored, cert_attribute)
            except AttributeError:
                self._gen_certs()
                return

    def _gen_certs(self):
        """Generate self-signed certs covering both KEDA Services and 127.0.0.1."""
        certs = gen_certs(
            service_name=self._metrics_apiserver_service,
            namespace=self.model.name,
            webhook_service=self._webhook_service,
        )
        for k, v in certs.items():
            setattr(self._stored, k, v)

    def _check_container_connection(self, container: Container) -> None:
        """Raise if a Pebble connection to the container cannot be made."""
        if not container.can_connect():
            raise ErrorWithStatus("Pod startup is not complete", MaintenanceStatus)

    def _upload_certs_to_container(self, container: Container, destination_path: str) -> None:
        """Push the shared serving certs into a container's cert directory."""
        self._check_container_connection(container)
        try:
            container.push(f"{destination_path}/tls.key", self._stored.key, make_dirs=True)
            container.push(f"{destination_path}/tls.crt", self._stored.cert, make_dirs=True)
            container.push(f"{destination_path}/ca.crt", self._stored.ca, make_dirs=True)
        except (ProtocolError, PathError) as e:
            raise GenericCharmRuntimeError("Failed to push certs to container") from e


if __name__ == "__main__":
    main(KedaCharm)
