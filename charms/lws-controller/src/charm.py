#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""LeaderWorkerSet (LWS) controller charm.

The charm packages and runs the upstream LWS controller-manager workload and
applies the matching CRD, RBAC and webhook resources to the cluster. It
publishes a simple readiness contract on the ``lws-controller`` relation so
that dependent charms (for example ``kserve-llmisvc``) can wait for LWS to be
reconciled before deploying workloads that need ``leaderworkersets``.
"""

import logging
from base64 import b64encode

import tenacity
import yaml
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
    BlockedStatus,
    Container,
    MaintenanceStatus,
    ModelError,
)
from ops.pebble import APIError, Layer, PathError, ProtocolError

from certs import gen_certs

log = logging.getLogger(__name__)

BASE_RESOURCE_FILES = [
    "src/templates/crd_manifests.yaml.j2",
    "src/templates/auth_manifests.yaml.j2",
    "src/templates/webhook_manifests.yaml.j2",
]

CONTAINER_CERTS_DEST = "/tmp/k8s-webhook-server/serving-certs"
# The upstream LWS image runs as a non-root user (distroless ``nonroot``)
# so we cannot write to ``/etc``. ``/tmp`` is writable by any user inside
# the container, which is sufficient because the file is read once at
# startup by the ``/manager`` binary.
MANAGER_CONFIG_DEST = "/tmp/lws/controller_manager_config.yaml"

LWS_SYNC_RELATION = "lws-controller"
METRICS_PORT = 8080
HEALTH_PORT = 8081

KRH_SCOPE_BASE = "lws-controller-base"

# Bounds for waiting on async resource deletion (CRD, webhooks, RBAC).
LWS_DELETION_TIMEOUT = 300
LWS_DELETION_POLL_INTERVAL = 5

# Controller-manager config the workload reads at startup via
# ``/manager --config=...``. Upstream delivers it as the ``manager-config``
# ConfigMap mounted into the pod; here the charm pushes it into the container
# with Pebble (see ``_upload_manager_config``). ``internalCertManagement.enable``
# is ``false`` (upstream default is ``true``) because the charm manages the
# webhook serving certs itself (see ``_gen_certs``); leaving it on would clash
# with the charm-managed certs/CABundle.
_MANAGER_CONFIG = """apiVersion: config.lws.x-k8s.io/v1alpha1
kind: Configuration
leaderElection:
  leaderElect: true
internalCertManagement:
  enable: false
"""


# For errors when a K8s object exists while it shouldn't
class ObjectStillExistsError(Exception):
    """Exception for when a K8s object exists, while it should have been removed."""

    def __init__(self, resource_name: str):
        self.resource_name = resource_name
        super().__init__(f"Resource still exists: {resource_name}")


class LWSControllerCharm(CharmBase):
    """Charm for the LeaderWorkerSet controller-manager."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self._base_resource_handler = None
        self._lightkube_field_manager = "lightkube"

        self._controller_container_name = "lws-controller"
        self._controller_service_name = self.app.name
        self._webhook_service_name = "lws-webhook-service"
        self.controller_container = self.unit.get_container(self._controller_container_name)

        self._gen_certs_if_missing()

        self._logging = LogForwarder(charm=self)

        for event in [
            self.on.install,
            self.on.config_changed,
            self.on.lws_controller_pebble_ready,
            self.on.leader_elected,
            self.on.update_status,
            self.on[LWS_SYNC_RELATION].relation_changed,
            self.on[LWS_SYNC_RELATION].relation_joined,
            self.on[LWS_SYNC_RELATION].relation_broken,
        ]:
            self.framework.observe(event, self._on_event)
        self.framework.observe(self.on.remove, self._on_remove)

        self.unit.set_ports(METRICS_PORT)
        self.prometheus_provider = MetricsEndpointProvider(
            self,
            jobs=[
                {
                    "job_name": "lws-controller",
                    "static_configs": [{"targets": [f"*:{METRICS_PORT}"]}],
                },
            ],
        )

    @property
    def _context(self):
        """Render context for base resource templates."""
        ca_context = b64encode(self._stored.ca.encode("ascii"))
        return {
            "app_name": self.app.name,
            "namespace": self.model.name,
            "webhook_service_name": self._webhook_service_name,
            "cert": f"'{ca_context.decode('utf-8')}'",
        }

    @property
    def base_resource_handler(self):
        """K8s handler for core lws resources (CRD, RBAC, webhooks)."""
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
    def _controller_pebble_layer(self):
        """Pebble layer for the LWS controller workload."""
        return Layer(
            {
                "services": {
                    self._controller_container_name: {
                        "override": "replace",
                        "summary": "LeaderWorkerSet Controller Manager",
                        "command": (
                            "/manager " f"--config={MANAGER_CONFIG_DEST} " "--zap-log-level=2"
                        ),
                        "startup": "enabled",
                        "environment": {
                            "POD_NAMESPACE": self.model.name,
                        },
                        "on-check-failure": {
                            "lws-controller-ready": "restart",
                            "lws-controller-alive": "restart",
                        },
                    },
                },
                "checks": {
                    "lws-controller-ready": {
                        "override": "replace",
                        "level": "ready",
                        "http": {"url": f"http://localhost:{HEALTH_PORT}/readyz"},
                    },
                    "lws-controller-alive": {
                        "override": "replace",
                        "level": "alive",
                        "http": {"url": f"http://localhost:{HEALTH_PORT}/healthz"},
                    },
                },
            }
        )

    def _on_event(self, event):
        """Main reconcile loop for the lws-controller charm."""
        try:
            self.unit.status = MaintenanceStatus("Creating k8s resources")

            manager_config = self._validated_manager_config()

            self._sync_handler_resource_types(self.base_resource_handler)
            self.base_resource_handler.apply()

            self._upload_certs_to_container(
                container=self.controller_container,
                destination_path=CONTAINER_CERTS_DEST,
                certs_store=self._stored,
            )
            self._upload_manager_config(self.controller_container, manager_config)

            update_layer(
                self._controller_container_name,
                self.controller_container,
                self._controller_pebble_layer,
                log,
            )
            self._restart_controller_service()

            self.unit.status = ActiveStatus()
            log.info("lws-controller reconcile complete; publishing ready=true")
            self._publish_lws_sync_data(ready=True)
        except ErrorWithStatus as err:
            self._publish_lws_sync_data(ready=False)
            self.model.unit.status = err.status
            log.error("Failed to handle %s with error: %s", event, err)
            return
        except ApiError:
            self._publish_lws_sync_data(ready=False)
            log.exception("Kubernetes API error during reconcile")
            raise

    @tenacity.retry(
        stop=tenacity.stop_after_delay(LWS_DELETION_TIMEOUT),
        wait=tenacity.wait_fixed(LWS_DELETION_POLL_INTERVAL),
        reraise=True,
    )
    def ensure_resource_is_deleted(
        self, client: Client, resource_kind, resource_name: str, namespace: str = None
    ):
        """Block until a resource no longer exists, retrying on each check.

        Deletion is async, so resources may linger after ``delete()`` returns;
        the workload must not be torn down until they are actually gone.

        Raises:
            ApiError: From lightkube, for any error other than 404.
            ObjectStillExistsError: If the resource still exists after retries.
        """
        log.info("Checking if resource exists: %s", resource_name)
        try:
            client.get(resource_kind, name=resource_name, namespace=namespace)
            log.info('Resource "%s" exists, retrying...', resource_name)
            raise ObjectStillExistsError(resource_name)
        except ApiError as e:
            if e.status.code == 404:
                log.info('Resource "%s" does not exist!', resource_name)
                return
            # Raise any other error
            raise

    def _ensure_all_deleted(self, client: Client, resources: list) -> None:
        """Wait for every resource to be deleted, aggregating any that linger.

        Each resource is checked independently so one stuck resource doesn't
        mask the others; raises a single ``ObjectStillExistsError`` listing all
        that remained.
        """
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
                log.warning(
                    "Resource %s still present after deletion; "
                    "continuing to check the remaining resources",
                    e.resource_name,
                )
                stuck.append(e.resource_name)
        if stuck:
            raise ObjectStillExistsError(", ".join(stuck))

    def _delete_base_resources_and_wait(self, client: Client, base_manifests: list) -> None:
        """Delete all base resources in finalizer-safe order and block until gone.

        The CRD is deleted first so its removal cascades to all CRs and lets the
        controller clear their finalizers; we wait for it to disappear before
        deleting the rest (webhooks, RBAC, Service), whose API endpoints survive
        CRD deletion. Deletion is async, so each phase blocks to avoid tearing
        the workload down while resources still linger.
        """
        if not base_manifests:
            return
        crd_manifests = [
            resource
            for resource in base_manifests
            if isinstance(resource, CustomResourceDefinition)
        ]
        non_crd_manifests = [
            resource
            for resource in base_manifests
            if not isinstance(resource, CustomResourceDefinition)
        ]

        if crd_manifests:
            delete_many(client, crd_manifests, ignore_missing=True, logger=log)
            self._ensure_all_deleted(client, crd_manifests)

        self.base_resource_handler.delete(ignore_missing=True)
        self._ensure_all_deleted(client, non_crd_manifests)

    def _on_remove(self, _):
        """Delete resources rendered by lws-controller handlers."""
        self.unit.status = MaintenanceStatus("Removing k8s resources")

        base_manifests = self._sync_handler_resource_types(self.base_resource_handler)

        client = self.base_resource_handler.lightkube_client
        try:
            # Delete the CRD first (cascading to its CRs) and wait, then the
            # remaining base resources. Block on each so the workload isn't torn
            # down while resources still linger.
            self._delete_base_resources_and_wait(client, base_manifests)
        except ApiError as e:
            if e.status.code != 404:
                log.warning("Failed to delete resources with error: %s", e)
                raise
        except ObjectStillExistsError as e:
            log.warning(
                "Failed to remove resource: %s. Manual intervention for cleanup might be required",
                e.resource_name,
            )
            raise
        self.unit.status = MaintenanceStatus("K8s resources removed")

    def _publish_lws_sync_data(self, ready: bool) -> None:
        """Publish readiness contract on every ``lws-controller`` relation."""
        if not self.unit.is_leader():
            return
        for relation in self.model.relations.get(LWS_SYNC_RELATION, []):
            relation.data[self.app].update(
                {
                    "ready": str(ready).lower(),
                    "namespace": self.model.name,
                }
            )

    def _gen_certs_if_missing(self) -> None:
        """Generate certs if they are not present in stored state."""
        log.info("Generating certificates if missing.")
        for cert_attribute in ["cert", "ca", "key"]:
            try:
                getattr(self._stored, cert_attribute)
                log.info("Certificate %s already exists, skipping generation.", cert_attribute)
            except AttributeError:
                self._gen_certs()
                return

    def _gen_certs(self):
        """Generate and persist self-signed certs for the webhook server."""
        log.info("Generating certificates..")
        certs = gen_certs(
            service_name=self._controller_service_name,
            namespace=self.model.name,
            webhook_service=self._webhook_service_name,
        )
        for k, v in certs.items():
            setattr(self._stored, k, v)

    def _check_container_connection(self, container: Container) -> None:
        """Check if a connection can be made with the container.

        Raises:
            ErrorWithStatus: if the connection cannot be made.
        """
        if not container.can_connect():
            raise ErrorWithStatus("Pod startup is not complete", MaintenanceStatus)

    def _upload_certs_to_container(
        self, container: Container, destination_path: str, certs_store: StoredState
    ) -> None:
        """Upload generated certs into the controller container."""
        self._check_container_connection(container)
        try:
            container.push(f"{destination_path}/tls.key", certs_store.key, make_dirs=True)
            container.push(f"{destination_path}/tls.crt", certs_store.cert, make_dirs=True)
            container.push(f"{destination_path}/ca.crt", certs_store.ca, make_dirs=True)
        except (ProtocolError, PathError) as e:
            raise GenericCharmRuntimeError("Failed to push certs to container") from e

    def _validated_manager_config(self) -> str:
        """Return the controller-manager config to push, validating user overrides.

        When the ``manager-config`` option is empty the built-in default is
        used. Otherwise the user-provided YAML fully replaces the default and
        must satisfy the cert invariants the charm relies on, raising
        ``ErrorWithStatus(BlockedStatus)`` when it does not.
        """
        raw = str(self.config.get("manager-config") or "").strip()
        if not raw:
            return _MANAGER_CONFIG

        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise ErrorWithStatus(f"manager-config is not valid YAML: {e}", BlockedStatus) from e

        if not isinstance(parsed, dict):
            raise ErrorWithStatus("manager-config must be a YAML mapping", BlockedStatus)

        internal_cert_management = parsed.get("internalCertManagement")
        if (
            not isinstance(internal_cert_management, dict)
            or internal_cert_management.get("enable") is not False
        ):
            raise ErrorWithStatus(
                "manager-config must set internalCertManagement.enable: false "
                "because the charm manages the webhook serving certificates",
                BlockedStatus,
            )

        webhook = parsed.get("webhook")
        if isinstance(webhook, dict) and "certDir" in webhook:
            if webhook["certDir"] != CONTAINER_CERTS_DEST:
                raise ErrorWithStatus(
                    f"manager-config webhook.certDir must be {CONTAINER_CERTS_DEST}",
                    BlockedStatus,
                )

        return raw

    def _upload_manager_config(self, container: Container, manager_config: str) -> None:
        """Push the LWS controller-manager configuration file into the container."""
        self._check_container_connection(container)
        try:
            container.push(MANAGER_CONFIG_DEST, manager_config, make_dirs=True)
        except (ProtocolError, PathError) as e:
            raise GenericCharmRuntimeError("Failed to push manager config to container") from e

    def _restart_controller_service(self) -> None:
        """Restart controller service when container and service are available."""
        if not self.controller_container.can_connect():
            log.info("Skipping service restart, controller container is not reachable")
            return

        try:
            self.controller_container.get_service(self._controller_container_name).is_running()
        except ModelError:
            log.info("Service not found, nothing to restart")
            return

        try:
            self.controller_container.restart(self._controller_container_name)
            log.info("Restarted the controller pebble service")
        except APIError as err:
            raise GenericCharmRuntimeError(
                f"Failed to restart {self._controller_container_name} service"
            ) from err


if __name__ == "__main__":
    main(LWSControllerCharm)
