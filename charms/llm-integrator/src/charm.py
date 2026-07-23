#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm that renders and manages a single KServe LLMInferenceService.

The charm has no workload container: it is a "rendering engine" that turns a
small set of Juju configuration options into a single ``LLMInferenceService``
custom resource, applies it to the cluster and keeps it reconciled. It is
gated on the ``kserve-llmisvc`` charm reporting ready via the
``kserve-llmisvc-sync`` relation.
"""

import logging
from urllib.parse import urlparse

import tenacity
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus
from charmed_kubeflow_chisme.kubernetes import (
    KubernetesResourceHandler,
    create_charm_default_labels,
)
from lightkube import ApiError
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Secret
from object_storage import S3Requirer
from ops import main
from ops.charm import CharmBase
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus,
    StatusBase,
    WaitingStatus,
)

log = logging.getLogger(__name__)

TEMPLATE_FILES = ["src/templates/llm_inference_service.yaml.j2"]
KRH_SCOPE = "llm-integrator"

# Readiness relation provided by the kserve-llmisvc charm. The relation name
# matches the provider charm name, following the repo convention where
# kserve-llmisvc itself requires ``kserve-controller`` / ``lws-controller``.
LLMISVC_SYNC_RELATION = "kserve-llmisvc"

# Relation to an s3-integrator that supplies the credentials for an s3:// model
# URI. It is optional: hf:// models do not need it.
S3_CREDENTIALS_RELATION = "s3-credentials"

# Supported model URI schemes.
HF_URI_PREFIX = "hf://"
S3_URI_PREFIX = "s3://"

# Default S3 region used when the s3-credentials relation does not provide one.
DEFAULT_S3_REGION = "us-east-1"

# How long to wait for the LLMInferenceService CR to finish terminating during
# removal. KServe attaches finalizers and tears down the underlying Deployments
# and pods asynchronously, so delete() returns before the CR is gone.
DELETION_TIMEOUT = 300
DELETION_POLL_INTERVAL = 5

# Registering the generic resource at import time adds it to lightkube's
# registry so the KubernetesResourceHandler codecs can (de)serialize it and so
# we can get()/delete() it directly.
LLMInferenceService = create_namespaced_resource(
    group="serving.kserve.io",
    version="v1alpha2",
    kind="LLMInferenceService",
    plural="llminferenceservices",
)


class ObjectStillExistsError(Exception):
    """Exception for when a K8s object exists, while it should have been removed."""

    def __init__(self, resource_name: str):
        self.resource_name = resource_name
        super().__init__(f"Resource still exists: {resource_name}")


class LLMIntegratorCharm(CharmBase):
    """Render and manage a single LLMInferenceService from Juju config."""

    def __init__(self, *args):
        super().__init__(*args)

        self._resource_handler = None
        self._lightkube_field_manager = self.app.name

        self.s3_requirer = S3Requirer(self, relation_name=S3_CREDENTIALS_RELATION)

        for event in [
            self.on.install,
            self.on.start,
            self.on.config_changed,
            self.on.leader_elected,
            self.on.update_status,
            self.on[LLMISVC_SYNC_RELATION].relation_changed,
            self.on[LLMISVC_SYNC_RELATION].relation_broken,
            self.on[S3_CREDENTIALS_RELATION].relation_changed,
            self.on[S3_CREDENTIALS_RELATION].relation_broken,
        ]:
            self.framework.observe(event, self._on_event)
        self.framework.observe(self.on.remove, self._on_remove)

    @property
    def _context(self):
        """Render context for the LLMInferenceService template.

        ``model-name`` is optional: when unset it defaults to the model
        reference derived from the URI. For hf:// URIs this is the part after
        the scheme (e.g. hf://EleutherAI/pythia-70m -> EleutherAI/pythia-70m);
        for s3:// URIs it is the last path segment of the bucket key (e.g.
        s3://my-bucket/models/pythia-70m -> pythia-70m). This is the identifier
        the model is served as through the OpenAI-compatible API.

        For s3:// URIs the context also carries the storage-initializer image
        and the S3 connection parameters used to render a manual
        storage-initializer init container.
        """
        model_uri = self._model_uri
        model_name = self.model.config.get("model-name", "").strip() or self._derived_model_name()
        context = {
            "app_name": self.app.name,
            "namespace": self.model.name,
            "model_uri": model_uri,
            "model_name": model_name,
            "runtime_image": self.model.config.get("runtime-image", "").strip(),
            "enable_prefill_decode": bool(self.model.config.get("enable-prefill-decode", True)),
            "is_s3": self._uri_scheme() == "s3",
        }
        if context["is_s3"]:
            context.update(self._s3_context())
        return context

    @property
    def _model_uri(self) -> str:
        """The configured model URI, stripped of surrounding whitespace."""
        return self.model.config.get("model-uri", "").strip()

    @property
    def _s3_secret_name(self) -> str:
        """Name of the Kubernetes Secret holding the S3 credentials.

        Stably derived from the app name (like the LLMInferenceService itself)
        so create/update/delete are all idempotent.
        """
        return f"{self.app.name}-s3-creds"

    def _uri_scheme(self) -> str:
        """Return the model URI scheme: ``hf``, ``s3`` or ``""`` when unknown."""
        if self._model_uri.startswith(HF_URI_PREFIX):
            return "hf"
        if self._model_uri.startswith(S3_URI_PREFIX):
            return "s3"
        return ""

    def _derived_model_name(self) -> str:
        """Derive the served model name from the URI when model-name is unset."""
        uri = self._model_uri
        if uri.startswith(HF_URI_PREFIX):
            return uri.removeprefix(HF_URI_PREFIX)
        if uri.startswith(S3_URI_PREFIX):
            # s3://bucket/path/to/model -> "model" (last non-empty segment).
            return uri.removeprefix(S3_URI_PREFIX).rstrip("/").rsplit("/", 1)[-1]
        return ""

    def _s3_connection_info(self) -> dict:
        """Return the s3-credentials connection info, or {} when unavailable."""
        relation = self.model.get_relation(S3_CREDENTIALS_RELATION)
        if relation is None:
            return {}
        return self.s3_requirer.get_storage_connection_info(relation) or {}

    def _s3_context(self) -> dict:
        """Build the storage-initializer render context from the s3 relation.

        The endpoint published on the relation is a URL (e.g.
        "https://s3.eu-central-1.amazonaws.com"); KServe's storage-initializer
        wants the host[:port] in ``S3_ENDPOINT`` and the scheme captured
        separately in ``S3_USE_HTTPS``.
        """
        info = self._s3_connection_info()
        parsed = urlparse(info.get("endpoint", ""))
        raw_endpoint = parsed.netloc or parsed.path
        endpoint = raw_endpoint.split("/", 1)[0]
        return {
            "storage_initializer_image": self.model.config.get(
                "storage-initializer-image", ""
            ).strip(),
            "s3_secret_name": self._s3_secret_name,
            "s3_endpoint": endpoint,
            "s3_use_https": "1" if parsed.scheme == "https" else "0",
            "s3_region": info.get("region") or DEFAULT_S3_REGION,
            "s3_access_key": info.get("access-key", ""),
            "s3_secret_access_key": info.get("secret-key", ""),
        }

    @property
    def resource_handler(self):
        """K8s handler for the LLMInferenceService resource."""
        if not self._resource_handler:
            self._resource_handler = KubernetesResourceHandler(
                field_manager=self._lightkube_field_manager,
                template_files=TEMPLATE_FILES,
                context=self._context,
                labels=create_charm_default_labels(
                    self.app.name, self.model.name, scope=KRH_SCOPE
                ),
                logger=log,
            )
        return self._resource_handler

    def _llmisvc_is_ready(self) -> bool:
        """Return True when the kserve-llmisvc relation reports ready=true."""
        relation = self.model.get_relation(LLMISVC_SYNC_RELATION)
        if relation is None or relation.app is None:
            return False
        app_data = relation.data.get(relation.app, {})
        return app_data.get("ready", "false").lower() == "true"

    def _validate_llmisvc_relation(self) -> None:
        """Validate relation presence and readiness from kserve-llmisvc.

        Missing relation is a user-actionable misconfiguration (Blocked).
        Present relation without ready=true is a convergence state (Waiting).
        """
        relation = self.model.get_relation(LLMISVC_SYNC_RELATION)
        if relation is None or relation.app is None:
            raise ErrorWithStatus(
                "Please relate to kserve-llmisvc:kserve-llmisvc",
                BlockedStatus,
            )
        if not self._llmisvc_is_ready():
            raise ErrorWithStatus(
                "Waiting for kserve-llmisvc to report ready=true",
                WaitingStatus,
            )

    def _validate_config(self) -> None:
        """Validate the charm configuration is complete and supported."""
        model_uri = self._model_uri
        if not model_uri:
            raise ErrorWithStatus("Missing required config: model-uri", BlockedStatus)
        if self._uri_scheme() == "":
            raise ErrorWithStatus(
                "model-uri must start with 'hf://' or 's3://'",
                BlockedStatus,
            )
        if not self.model.config.get("runtime-image", "").strip():
            raise ErrorWithStatus("Missing required config: runtime-image", BlockedStatus)
        if (
            self._uri_scheme() == "s3"
            and not self.model.config.get("storage-initializer-image", "").strip()
        ):
            raise ErrorWithStatus(
                "Missing required config: storage-initializer-image", BlockedStatus
            )

    def _validate_s3(self) -> None:
        """Validate the s3-credentials relation when the model URI is s3://.

        Missing relation is a user-actionable misconfiguration (Blocked).
        Present relation without usable credentials is a convergence state
        (Waiting). Non-s3 URIs need no S3 relation and short-circuit.
        """
        if self._uri_scheme() != "s3":
            return
        relation = self.model.get_relation(S3_CREDENTIALS_RELATION)
        if relation is None:
            raise ErrorWithStatus(
                "Please relate to an s3-integrator over s3-credentials to use an "
                "s3:// model-uri",
                BlockedStatus,
            )
        info = self._s3_connection_info()
        missing = {"access-key", "secret-key", "endpoint"} - set(info)
        if missing:
            raise ErrorWithStatus(
                "Waiting for s3-credentials relation data "
                f"(missing: {', '.join(sorted(missing))})",
                WaitingStatus,
            )

    def _llm_isvc_status(self) -> StatusBase:
        """Derive the charm status from the LLMInferenceService Ready condition.

        The LLMInferenceService exposes a Knative-style aggregate ``Ready``
        condition whose ``status`` encodes whether the workload needs user
        intervention:

        - ``True``: serving -> ActiveStatus.
        - ``Unknown`` (or no status yet): still reconciling (pulling the model,
          scaling, deployment progressing) -> WaitingStatus. Recoverable without
          user action.
        - ``False``: a dependency hard-failed (bad image, model not found,
          unschedulable, invalid spec) -> BlockedStatus. Will not recover
          without user intervention; the condition message is surfaced so the
          operator knows what to fix.
        """
        name = self.app.name
        client = self.resource_handler.lightkube_client
        try:
            obj = client.get(LLMInferenceService, name=name, namespace=self.model.name)
        except ApiError as e:
            if e.status.code == 404:
                return WaitingStatus(f"Waiting for LLMInferenceService {name} to be created")
            raise

        status = getattr(obj, "status", None) or {}
        ready = next(
            (c for c in status.get("conditions", []) if c.get("type") == "Ready"),
            None,
        )
        if ready is None:
            return WaitingStatus(f"Waiting for LLMInferenceService {name} to report status")

        ready_status = ready.get("status")
        if ready_status == "True":
            return ActiveStatus()

        detail = ready.get("message") or ready.get("reason") or "reason unknown"
        if ready_status == "False":
            return BlockedStatus(
                f"LLMInferenceService {name} failed: {detail}. " "Manual intervention is required."
            )
        # Unknown / transient: still progressing, recoverable without action.
        return WaitingStatus(f"Waiting for LLMInferenceService {name} to become Ready: {detail}")

    def _on_event(self, event) -> None:
        """Main reconcile loop for the llm-integrator charm."""
        try:
            self._validate_llmisvc_relation()
            self._validate_config()
            self._validate_s3()

            self.unit.status = MaintenanceStatus("Applying LLMInferenceService")
            self.resource_handler.apply()

            self.unit.status = self._llm_isvc_status()
        except ErrorWithStatus as err:
            self.unit.status = err.status
            log.error("Failed to handle %s with error: %s", event, err)
            return
        except ApiError:
            log.exception("Kubernetes API error during reconcile")
            raise

    @tenacity.retry(
        stop=tenacity.stop_after_delay(DELETION_TIMEOUT),
        wait=tenacity.wait_fixed(DELETION_POLL_INTERVAL),
        reraise=True,
    )
    def _ensure_resource_is_deleted(self, client, resource_kind, resource_name, namespace):
        """Block until a resource no longer exists, retrying on each check."""
        try:
            client.get(resource_kind, name=resource_name, namespace=namespace)
            log.info('Resource "%s" still exists, retrying...', resource_name)
            raise ObjectStillExistsError(resource_name)
        except ApiError as e:
            if e.status.code == 404:
                log.info('Resource "%s" does not exist.', resource_name)
                return
            raise

    def _delete_resource(self, client, resource_type, name) -> None:
        """Delete a namespaced resource by name, tolerating it already being gone.

        A missing object (404) or a missing CRD ("no matches for kind") is
        treated as success so cleanup is idempotent and robust to the
        kserve-llmisvc charm having been removed first.
        """
        kind = getattr(resource_type, "__name__", str(resource_type))
        try:
            client.delete(resource_type, name=name, namespace=self.model.name)
        except ApiError as e:
            if e.status.code == 404 or "no matches for kind" in e.status.message:
                log.info("%s %s already gone; nothing to delete.", kind, name)
                return
            log.warning("Failed to delete %s %s with error: %s", kind, name, e)
            raise

    def _on_remove(self, _) -> None:
        """Delete everything the charm created and wait for full teardown.

        The charm owns the ``LLMInferenceService`` CR and, for s3:// models, the
        credentials ``Secret``. Both are requested for deletion up front (so the
        Secret is removed even if the CR teardown is slow), then we wait for the
        CR to actually disappear (KServe finalizers tear the workload down
        asynchronously). The Secret has no finalizers and is removed immediately.
        """
        self.unit.status = MaintenanceStatus("Removing k8s resources")
        client = self.resource_handler.lightkube_client

        self._delete_resource(client, LLMInferenceService, self.app.name)
        # Always attempt the Secret delete (tolerating 404) so nothing is left
        # behind even if the model-uri was switched away from s3:// beforehand.
        self._delete_resource(client, Secret, self._s3_secret_name)

        try:
            self._ensure_resource_is_deleted(
                client, LLMInferenceService, self.app.name, self.model.name
            )
        except ObjectStillExistsError as e:
            log.warning(
                "Failed to remove resource: %s. Manual intervention for cleanup might be required",
                e.resource_name,
            )
            raise
        self.unit.status = MaintenanceStatus("K8s resources removed")


if __name__ == "__main__":
    main(LLMIntegratorCharm)
