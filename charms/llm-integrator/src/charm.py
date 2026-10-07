#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm that renders and manages a single KServe LLMInferenceService.

The charm has no workload container: it is a "rendering engine" that turns a
small set of Juju configuration options into a single ``LLMInferenceService``
custom resource, applies it to the cluster and keeps it reconciled. It is
gated on the ``kserve-llmisvc`` charm reporting ready via the
``kserve-llmisvc-sync`` relation. Optionally it autoscales the workers with
KEDA ScaledObjects driven by vLLM metrics from a related Prometheus.
"""

import logging
from functools import cached_property
from typing import List
from urllib.parse import urlparse

import httpx
import tenacity
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus
from charmed_kubeflow_chisme.kubernetes import (
    KubernetesResourceHandler,
    create_charm_default_labels,
)
from charms.mimir_coordinator_k8s.v0.prometheus_api import PrometheusApiRequirer
from lightkube import ApiError
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Node, Pod, Secret
from object_storage import S3Requirer
from ops import main
from ops.charm import CharmBase, SecretChangedEvent
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus,
    ModelError,
    SecretNotFoundError,
    StatusBase,
    WaitingStatus,
)
from pydantic import ValidationError

from autoscaling import scaled_objects_context, worker_deployments
from capacity import WorkerRequest, find_capacity_issue
from config import CharmConfig, validation_error_message
from workload_status import diagnose_workload

log = logging.getLogger(__name__)

TEMPLATE_FILES = ["src/templates/llm_inference_service.yaml.j2"]
SCALED_OBJECTS_TEMPLATE_FILES = ["src/templates/scaled_objects.yaml.j2"]
KRH_SCOPE = "llm-integrator"
SCALED_OBJECTS_KRH_SCOPE = "llm-integrator-autoscaling"

# Readiness relation provided by the kserve-llmisvc charm. The relation name
# matches the provider charm name, following the repo convention where
# kserve-llmisvc itself requires ``kserve-controller`` / ``lws-controller``.
LLMISVC_SYNC_RELATION = "kserve-llmisvc"

# Relation to an s3-integrator that supplies the credentials for an s3:// model
# URI. It is optional: hf:// models do not need it.
S3_CREDENTIALS_RELATION = "s3-credentials"

# Readiness relation provided by the keda-controller charm; required for autoscaling.
KEDA_RELATION = "keda"

# Relation to the Prometheus that scrapes the vLLM metrics; required for autoscaling.
PROMETHEUS_API_RELATION = "prometheus-api"

# Default S3 region used when the s3-credentials relation does not provide one.
DEFAULT_S3_REGION = "us-east-1"

# Field name expected inside the Juju user secret referenced by hf-token-secret.
HF_TOKEN_SECRET_KEY = "token"

# How long to wait for the LLMInferenceService CR to finish terminating during
# removal. KServe attaches finalizers and tears down the underlying Deployments
# and pods asynchronously, so delete() returns before the CR is gone.
DELETION_TIMEOUT = 300
DELETION_POLL_INTERVAL = 5

# Ready condition reason KServe reports both while the workload starts and when it is broken.
WORKLOAD_UNAVAILABLE_REASON = "MinimumReplicasUnavailable"

# /dev/shm size per GPU for tensor-parallel workers; the presets' 1Gi suffices for one GPU.
# 2Gi follows vLLM's Kubernetes guide: https://docs.vllm.ai/en/latest/deployment/k8s.html
SHM_GI_PER_GPU = 2

# Registering the generic resources at import time adds them to lightkube's
# registry so the KubernetesResourceHandler codecs can (de)serialize them and so
# we can get()/delete() them directly.
LLMInferenceService = create_namespaced_resource(
    group="serving.kserve.io",
    version="v1alpha2",
    kind="LLMInferenceService",
    plural="llminferenceservices",
)
ScaledObject = create_namespaced_resource(
    group="keda.sh", version="v1alpha1", kind="ScaledObject", plural="scaledobjects"
)


class ObjectStillExistsError(Exception):
    """Exception for when a K8s object exists, while it should have been removed."""

    def __init__(self, resource_name: str):
        self.resource_name = resource_name
        super().__init__(f"Resource still exists: {resource_name}")


def _is_secret_permission_denied(exc: BaseException) -> bool:
    """True when a ModelError signals the secret exists but was not granted."""
    return isinstance(exc, ModelError) and "permission denied" in str(exc).lower()


class LLMIntegratorCharm(CharmBase):
    """Render and manage a single LLMInferenceService from Juju config."""

    def __init__(self, *args):
        super().__init__(*args)

        self._resource_handler = None
        self._scaled_objects_handler = None
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
            self.on[KEDA_RELATION].relation_changed,
            self.on[KEDA_RELATION].relation_broken,
            self.on[PROMETHEUS_API_RELATION].relation_changed,
            self.on[PROMETHEUS_API_RELATION].relation_broken,
            self.on.secret_changed,
        ]:
            self.framework.observe(event, self._on_event)
        self.framework.observe(self.on.remove, self._on_remove)

    @cached_property
    def _config(self) -> CharmConfig:
        """The validated charm config; invalid config blocks the charm."""
        try:
            return self.load_config(CharmConfig)
        except ValidationError as err:
            raise ErrorWithStatus(validation_error_message(err), BlockedStatus)

    @property
    def _context(self):
        """Render context for the LLMInferenceService template.

        The model is always downloaded by a storage-initializer init container
        rendered by the charm; s3:// URIs add the S3 connection parameters and
        gated hf:// models the Hugging Face token Secret.

        Only build it once the configuration has been validated.
        """
        config = self._config
        context = {
            "app_name": self.app.name,
            "namespace": self.model.name,
            "model_uri": config.model_uri,
            "model_name": config.served_model_name,
            "storage_initializer_image": config.effective_storage_initializer_image,
            "enable_prefill_decode": config.enable_prefill_decode,
            "is_s3": config.uri_scheme == "s3",
            "use_hf_token": self._use_hf_token,
            **self._worker_context(),
        }
        if context["is_s3"]:
            context.update(self._s3_context())
        elif context["use_hf_token"]:
            context.update(self._hf_context())
        return context

    def _shared_vllm_args(self) -> List[str]:
        """vLLM arguments applied to both the decode and the prefill worker."""
        config = self._config
        args = []
        if config.worker_gpus > 1:
            args += ["--tensor-parallel-size", str(config.worker_gpus)]
        if config.max_model_len:
            args += ["--max-model-len", str(config.max_model_len)]
        if config.is_gpu and config.gpu_memory_utilization:
            args += ["--gpu-memory-utilization", str(config.gpu_memory_utilization)]
        return args + config.vllm_extra_args

    def _worker_context(self) -> dict:
        """Render context for the vLLM worker pods."""
        config = self._config
        gpu_count = config.worker_gpus
        shared_args = self._shared_vllm_args()
        decode_args = [] if config.is_gpu else ["--enforce-eager"]
        if config.enable_prefill_decode:
            # The routing sidecar injected in disaggregated mode owns port 8000.
            decode_args += ["--port", "8001"]
        return {
            "runtime_image": config.effective_runtime_image,
            "is_gpu": config.is_gpu,
            "gpu_count": gpu_count,
            "resources": config.worker_resources,
            "shm_size": f"{SHM_GI_PER_GPU * gpu_count}Gi" if gpu_count > 1 else "",
            "decode_args": decode_args + shared_args,
            "prefill_args": ["--enable-chunked-prefill", *shared_args],
        }

    @property
    def _s3_secret_name(self) -> str:
        """Name of the Kubernetes Secret holding the S3 credentials.

        Stably derived from the app name (like the LLMInferenceService itself)
        so create/update/delete are all idempotent.
        """
        return f"{self.app.name}-s3-creds"

    @property
    def _hf_secret_name(self) -> str:
        """Name of the Kubernetes Secret holding the Hugging Face token.

        Stably derived from the app name so create/update/delete are idempotent.
        """
        return f"{self.app.name}-hf-token"

    @property
    def _hf_token_secret_id(self) -> str:
        """URI of the Juju user secret configured via hf-token-secret."""
        return self.model.config.get("hf-token-secret", "").strip()

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
            "s3_secret_name": self._s3_secret_name,
            "s3_endpoint": endpoint,
            "s3_use_https": "1" if parsed.scheme == "https" else "0",
            "s3_region": info.get("region") or DEFAULT_S3_REGION,
            "s3_access_key": info.get("access-key", ""),
            "s3_secret_access_key": info.get("secret-key", ""),
        }

    @cached_property
    def _hf_token(self) -> str:
        """The Hugging Face token, or "" when unset/unavailable; read once per hook.

        Reads the Juju user secret referenced by hf-token-secret. Tolerant by
        design: an unset, ungranted or malformed secret yields "" so the render
        context can be built without raising; ``_validate_hf_token`` surfaces the
        actionable error to the operator.
        """
        secret_id = self._hf_token_secret_id
        if not secret_id:
            return ""
        try:
            secret = self.model.get_secret(id=secret_id)
            return secret.get_content(refresh=True).get(HF_TOKEN_SECRET_KEY, "")
        except (SecretNotFoundError, ModelError):
            return ""

    @tenacity.retry(
        retry=tenacity.retry_if_exception(_is_secret_permission_denied),
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_fixed(5),
        reraise=True,
    )
    def _fetch_hf_secret_content(self, secret_id: str) -> dict:
        """Read the hf-token-secret content, retrying briefly on a grant race.

        ``juju config`` and ``juju grant-secret`` are separate commands, so a
        config-changed hook can fire before the grant lands; a short retry
        absorbs that window.
        """
        return self.model.get_secret(id=secret_id).get_content(refresh=True)

    @property
    def _use_hf_token(self) -> bool:
        """True when an hf:// model should be served with a Hugging Face token."""
        return self._config.uri_scheme == "hf" and bool(self._hf_token)

    def _hf_context(self) -> dict:
        """Build the Hugging Face token render context for a gated hf:// model."""
        return {
            "hf_secret_name": self._hf_secret_name,
            "hf_token": self._hf_token,
        }

    @property
    def resource_handler(self):
        """K8s handler for the LLMInferenceService resource."""
        if not self._resource_handler:
            self._resource_handler = KubernetesResourceHandler(
                field_manager=self._lightkube_field_manager,
                template_files=TEMPLATE_FILES,
                labels=create_charm_default_labels(
                    self.app.name, self.model.name, scope=KRH_SCOPE
                ),
                logger=log,
            )
        return self._resource_handler

    @property
    def scaled_objects_handler(self):
        """K8s handler for the KEDA ScaledObjects."""
        if not self._scaled_objects_handler:
            self._scaled_objects_handler = KubernetesResourceHandler(
                field_manager=self._lightkube_field_manager,
                template_files=SCALED_OBJECTS_TEMPLATE_FILES,
                labels=create_charm_default_labels(
                    self.app.name, self.model.name, scope=SCALED_OBJECTS_KRH_SCOPE
                ),
                logger=log,
            )
        return self._scaled_objects_handler

    def _validate_ready_relation(
        self, relation_name: str, provider: str, blocked_message: str
    ) -> None:
        """Block without the relation (user action); wait until the provider reports ready."""
        relation = self.model.get_relation(relation_name)
        if relation is None or relation.app is None:
            raise ErrorWithStatus(blocked_message, BlockedStatus)
        if relation.data[relation.app].get("ready", "false").lower() != "true":
            raise ErrorWithStatus(f"Waiting for {provider} to report ready=true", WaitingStatus)

    def _prometheus_url(self) -> str:
        """In-cluster URL of the related Prometheus that KEDA queries."""
        if self.model.get_relation(PROMETHEUS_API_RELATION) is None:
            raise ErrorWithStatus(
                f"Please relate to Prometheus over {PROMETHEUS_API_RELATION} "
                "to enable autoscaling",
                BlockedStatus,
            )
        try:
            data = PrometheusApiRequirer(self.model.relations, PROMETHEUS_API_RELATION).get_data()
        except ValidationError:
            data = None
        if data is None:
            raise ErrorWithStatus(
                f"Waiting for {PROMETHEUS_API_RELATION} relation data", WaitingStatus
            )
        # KEDA appends /api/v1/query to the address itself.
        return str(data.direct_url).rstrip("/")

    def _validate_cluster_capacity(self, config: CharmConfig) -> None:
        """Block when no node could ever schedule the workers, e.g. no GPUs in the cluster."""
        resources = config.worker_resources
        replicas = config.min_replicas if config.enable_autoscaling else 1
        roles = 2 if config.enable_prefill_decode else 1
        request = WorkerRequest(
            cpu=resources["cpu-request"],
            memory=resources["memory-request"],
            gpus=config.worker_gpus,
            workers=replicas * roles,
        )
        nodes = self.resource_handler.lightkube_client.list(Node)
        if issue := find_capacity_issue(nodes, request):
            raise ErrorWithStatus(issue, BlockedStatus)

    def _validate_hf_token(self) -> None:
        """Validate the hf-token-secret configuration when set.

        The token is optional (public hf:// models need none). When configured
        it must reference a granted Juju secret that carries the token under the
        expected key, and only makes sense for an hf:// model URI. Every failure
        here is a user-actionable misconfiguration (Blocked).
        """
        secret_id = self._hf_token_secret_id
        if not secret_id:
            return
        if self._config.uri_scheme != "hf":
            raise ErrorWithStatus(
                "hf-token-secret is only supported with an hf:// model-uri",
                BlockedStatus,
            )
        try:
            content = self._fetch_hf_secret_content(secret_id)
        except SecretNotFoundError:
            raise ErrorWithStatus(
                f"HF token secret {secret_id} does not exist",
                BlockedStatus,
            )
        except ModelError as err:
            if _is_secret_permission_denied(err):
                raise ErrorWithStatus(
                    f"HF token secret {secret_id} not granted to this app; run "
                    f"'juju grant-secret <secret> {self.app.name}'",
                    BlockedStatus,
                )
            raise ErrorWithStatus(
                f"Could not read HF token secret {secret_id}",
                BlockedStatus,
            )
        if not content.get(HF_TOKEN_SECRET_KEY):
            raise ErrorWithStatus(
                f"HF token secret must contain a '{HF_TOKEN_SECRET_KEY}' key",
                BlockedStatus,
            )

    def _validate_s3(self) -> None:
        """Validate the s3-credentials relation when the model URI is s3://.

        Missing relation is a user-actionable misconfiguration (Blocked).
        Present relation without usable credentials is a convergence state
        (Waiting). Non-s3 URIs need no S3 relation and short-circuit.
        """
        if self._config.uri_scheme != "s3":
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
          operator knows what to fix. The exception is an unavailable workload,
          which KServe also reports while the model downloads and loads; the
          workload pods decide between Waiting and Blocked.
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
        if ready_status == "False" and ready.get("reason") == WORKLOAD_UNAVAILABLE_REASON:
            return self._workload_status(name)

        detail = ready.get("message") or ready.get("reason") or "reason unknown"
        if ready_status == "False":
            return BlockedStatus(
                f"LLMInferenceService {name} failed: {detail}. " "Manual intervention is required."
            )
        # Unknown / transient: still progressing, recoverable without action.
        return WaitingStatus(f"Waiting for LLMInferenceService {name} to become Ready: {detail}")

    def _workload_status(self, name: str) -> StatusBase:
        """Tell a starting workload (Waiting) from a failing one (Blocked) via its pods."""
        pods = self.resource_handler.lightkube_client.list(
            Pod,
            namespace=self.model.name,
            labels={"app.kubernetes.io/name": name, "kserve.io/component": "workload"},
        )
        state = diagnose_workload(pods)
        if state.failed:
            return BlockedStatus(
                f"LLMInferenceService {name} failed: {state.message}. "
                "Manual intervention is required."
            )
        return WaitingStatus(f"LLMInferenceService {name} is starting: {state.message}")

    def _reconcile_autoscaling(self, config: CharmConfig) -> StatusBase:
        """Create, update or remove the ScaledObjects and return the autoscaling status.

        A missing or unready KEDA or Prometheus never stops serving: the workers keep their
        current replica count and only the returned status reports what is missing.
        """
        wanted: List[str] = []
        status: StatusBase = ActiveStatus()
        if config.enable_autoscaling:
            try:
                self._validate_ready_relation(
                    KEDA_RELATION,
                    "keda-controller",
                    f"Please relate to keda-controller:{KEDA_RELATION} to enable autoscaling",
                )
                server_address = self._prometheus_url()
            except ErrorWithStatus as err:
                status = err.status
            else:
                context = scaled_objects_context(
                    self.app.name, self.model.name, config, server_address
                )
                self.scaled_objects_handler.context = context
                self.scaled_objects_handler.apply()
                wanted = [scaled_object["name"] for scaled_object in context["scaled_objects"]]
                status = self._scaled_objects_status(config, wanted)

        # Deleting by name works whether or not KEDA (and so the ScaledObject CRD) is installed.
        client = self.resource_handler.lightkube_client
        for name in worker_deployments(self.app.name, prefill_decode=True):
            if name not in wanted:
                self._delete_resource(client, ScaledObject, name)
        return status

    def _scaled_objects_status(self, config: CharmConfig, names: List[str]) -> StatusBase:
        """Report a ScaledObject KEDA marks not ready, e.g. because its metric query fails."""
        client = self.scaled_objects_handler.lightkube_client
        for name in names:
            obj = client.get(ScaledObject, name=name, namespace=self.model.name)
            conditions = (getattr(obj, "status", None) or {}).get("conditions", [])
            ready = next((c for c in conditions if c.get("type") == "Ready"), {})
            if ready.get("status") == "False":
                detail = ready.get("message") or ready.get("reason") or "reason unknown"
                return WaitingStatus(f"Autoscaling of {name} is not working: {detail}")
        return ActiveStatus(f"Autoscaling {config.min_replicas}-{config.max_replicas} replicas")

    def _on_event(self, event) -> None:
        """Main reconcile loop for the llm-integrator charm."""
        # Ignore secret-changed notifications for secrets we do not consume.
        if isinstance(event, SecretChangedEvent) and (
            not self._hf_token_secret_id or event.secret.id != self._hf_token_secret_id
        ):
            return
        try:
            self._validate_ready_relation(
                LLMISVC_SYNC_RELATION,
                "kserve-llmisvc",
                f"Please relate to kserve-llmisvc:{LLMISVC_SYNC_RELATION}",
            )
            config = self._config
            self._validate_hf_token()
            self._validate_s3()
            self._validate_cluster_capacity(config)

            self.unit.status = MaintenanceStatus("Applying LLMInferenceService")
            self.resource_handler.context = self._context
            self.resource_handler.apply()

            # Prune the HF token Secret when the token is no longer in use (e.g.
            # the config was cleared) since apply() does not remove it.
            if not self._use_hf_token:
                self._delete_resource(
                    self.resource_handler.lightkube_client, Secret, self._hf_secret_name
                )

            workload_status = self._llm_isvc_status()
            autoscaling_status = self._reconcile_autoscaling(config)
            # Autoscaling problems only show once the workload itself is serving.
            self.unit.status = (
                autoscaling_status
                if isinstance(workload_status, ActiveStatus)
                else workload_status
            )
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

        A missing object (404) or a missing CRD is treated as success so cleanup
        is idempotent and robust to kserve-llmisvc or KEDA not being installed.
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
        except httpx.HTTPStatusError as e:
            # An API group that is not served answers with a plain-text 404 lightkube can't parse.
            if e.response.status_code == 404:
                log.info("%s API not served; no %s to delete.", kind, name)
                return
            raise

    def _on_remove(self, _) -> None:
        """Delete everything the charm created and wait for full teardown.

        The charm owns the ``LLMInferenceService`` CR, the KEDA ``ScaledObjects``
        and, for s3:// or gated hf:// models, a credentials ``Secret``. The
        ScaledObjects go first so KEDA stops scaling the workers being torn down.
        Everything is requested for deletion up front (so the Secret is removed
        even if the CR teardown is slow), then we wait for the CR to actually
        disappear (KServe finalizers tear the workload down asynchronously).
        """
        self.unit.status = MaintenanceStatus("Removing k8s resources")
        client = self.resource_handler.lightkube_client

        for name in worker_deployments(self.app.name, prefill_decode=True):
            self._delete_resource(client, ScaledObject, name)
        self._delete_resource(client, LLMInferenceService, self.app.name)
        # Always attempt the Secret deletes (tolerating 404) so nothing is left
        # behind even if the model-uri was switched away from s3:///hf:// first.
        self._delete_resource(client, Secret, self._s3_secret_name)
        self._delete_resource(client, Secret, self._hf_secret_name)

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
