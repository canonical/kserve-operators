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

import json
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlparse

import tenacity
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus
from charmed_kubeflow_chisme.kubernetes import (
    KubernetesResourceHandler,
    create_charm_default_labels,
)
from lightkube import ApiError
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Node, Secret
from lightkube.utils.quantity import parse_quantity
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

from capacity import WorkerRequest, find_capacity_issue

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

# Field name expected inside the Juju user secret referenced by hf-token-secret.
HF_TOKEN_SECRET_KEY = "token"

# How long to wait for the LLMInferenceService CR to finish terminating during
# removal. KServe attaches finalizers and tears down the underlying Deployments
# and pods asynchronously, so delete() returns before the CR is gone.
DELETION_TIMEOUT = 300
DELETION_POLL_INTERVAL = 5

DEFAULT_IMAGES = json.loads((Path(__file__).parent / "default-custom-images.json").read_text())

CPU = "cpu"
NVIDIA_GPU = "nvidia-gpu"

# Worker resource options; empty values fall back to the accelerator defaults.
RESOURCE_OPTIONS = ("cpu-request", "cpu-limit", "memory-request", "memory-limit")


@dataclass(frozen=True)
class AcceleratorDefaults:
    """Defaults for an accelerator, used when the matching config option is empty."""

    image: str
    resources: Dict[str, str]


ACCELERATOR_DEFAULTS = {
    CPU: AcceleratorDefaults(
        image=DEFAULT_IMAGES["vllm"],
        resources={
            "cpu-request": "500m",
            "cpu-limit": "2",
            "memory-request": "4Gi",
            "memory-limit": "8Gi",
        },
    ),
    NVIDIA_GPU: AcceleratorDefaults(
        image=DEFAULT_IMAGES["vllm_gpu"],
        resources={
            "cpu-request": "2",
            "cpu-limit": "4",
            "memory-request": "8Gi",
            "memory-limit": "16Gi",
        },
    ),
}

# /dev/shm size per GPU for tensor-parallel workers; the presets' 1Gi suffices for one GPU.
SHM_GI_PER_GPU = 2

# vLLM flags that vllm-extra-args may not set, with the reason surfaced to the user.
RESERVED_VLLM_FLAGS = {
    "--port": "it is managed by the charm",
    "--served-model-name": "use the model-name option",
    "--tensor-parallel-size": "use the gpu-count option",
    "-tp": "use the gpu-count option",
    "--max-model-len": "use the max-model-len option",
    "--gpu-memory-utilization": "use the gpu-memory-utilization option",
    "--kv-transfer-config": "KV-cache transfer is not supported yet",
}

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


def _is_secret_permission_denied(exc: BaseException) -> bool:
    """True when a ModelError signals the secret exists but was not granted."""
    return isinstance(exc, ModelError) and "permission denied" in str(exc).lower()


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
            self.on.secret_changed,
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

        Only build it once the configuration has been validated.
        """
        model_uri = self._model_uri
        model_name = self.model.config.get("model-name", "").strip() or self._derived_model_name()
        context = {
            "app_name": self.app.name,
            "namespace": self.model.name,
            "model_uri": model_uri,
            "model_name": model_name,
            "enable_prefill_decode": self._enable_prefill_decode,
            "is_s3": self._uri_scheme() == "s3",
            "use_hf_token": self._use_hf_token(),
            **self._worker_context(),
        }
        if context["is_s3"]:
            context.update(self._s3_context())
        elif context["use_hf_token"]:
            context.update(self._hf_context())
        return context

    @property
    def _model_uri(self) -> str:
        """The configured model URI, stripped of surrounding whitespace."""
        return self.model.config.get("model-uri", "").strip()

    @property
    def _enable_prefill_decode(self) -> bool:
        return self.model.config["enable-prefill-decode"]

    @property
    def _accelerator(self) -> str:
        return self.model.config["accelerator"].strip()

    @property
    def _is_gpu(self) -> bool:
        return self._accelerator == NVIDIA_GPU

    @property
    def _gpu_count(self) -> int:
        """GPUs requested per worker; 0 for CPU workloads."""
        return self.model.config["gpu-count"] if self._is_gpu else 0

    @property
    def _worker_resources(self) -> Dict[str, str]:
        """Per-worker resource quantities keyed by config option name."""
        defaults = ACCELERATOR_DEFAULTS[self._accelerator].resources
        return {
            option: self.model.config[option].strip() or defaults[option]
            for option in RESOURCE_OPTIONS
        }

    def _vllm_extra_args(self) -> List[str]:
        """Parse vllm-extra-args, rejecting flags the charm reserves."""
        try:
            args = shlex.split(self.model.config["vllm-extra-args"])
        except ValueError as err:
            raise ErrorWithStatus(f"Invalid vllm-extra-args: {err}", BlockedStatus)
        for arg in args:
            # vLLM accepts --flag=value and underscores in flag names.
            flag = arg.split("=", 1)[0].replace("_", "-")
            if flag in RESERVED_VLLM_FLAGS:
                raise ErrorWithStatus(
                    f"vllm-extra-args must not set {flag}: {RESERVED_VLLM_FLAGS[flag]}",
                    BlockedStatus,
                )
        return args

    def _shared_vllm_args(self) -> List[str]:
        """vLLM arguments applied to both the decode and the prefill worker."""
        args = []
        if self._gpu_count > 1:
            args += ["--tensor-parallel-size", str(self._gpu_count)]
        if max_model_len := self.model.config["max-model-len"]:
            args += ["--max-model-len", str(max_model_len)]
        gpu_memory_utilization = self.model.config["gpu-memory-utilization"]
        if self._is_gpu and gpu_memory_utilization:
            args += ["--gpu-memory-utilization", str(gpu_memory_utilization)]
        return args + self._vllm_extra_args()

    def _worker_context(self) -> dict:
        """Render context for the vLLM worker pods."""
        gpu_count = self._gpu_count
        shared_args = self._shared_vllm_args()
        decode_args = [] if self._is_gpu else ["--enforce-eager"]
        if self._enable_prefill_decode:
            # The routing sidecar injected in disaggregated mode owns port 8000.
            decode_args += ["--port", "8001"]
        return {
            "runtime_image": self.model.config["runtime-image"].strip()
            or ACCELERATOR_DEFAULTS[self._accelerator].image,
            "is_gpu": self._is_gpu,
            "gpu_count": gpu_count,
            "resources": self._worker_resources,
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

    def _hf_token(self) -> str:
        """Return the Hugging Face token, or "" when unset/unavailable.

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

    def _use_hf_token(self) -> bool:
        """True when an hf:// model should be served with a Hugging Face token."""
        return self._uri_scheme() == "hf" and bool(self._hf_token())

    def _hf_context(self) -> dict:
        """Build the storage-initializer render context for a gated hf:// model."""
        return {
            "storage_initializer_image": self.model.config.get(
                "storage-initializer-image", ""
            ).strip(),
            "hf_secret_name": self._hf_secret_name,
            "hf_token": self._hf_token(),
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
        # An hf-token-secret on an hf:// model makes the charm render a manual
        # storage-initializer, so the image is required. Key this off config
        # alone (no secret read) so a missing image is reported directly instead
        # of being hidden until the secret is granted.
        hf_token_configured = self._uri_scheme() == "hf" and bool(self._hf_token_secret_id)
        needs_storage_initializer = self._uri_scheme() == "s3" or hf_token_configured
        if (
            needs_storage_initializer
            and not self.model.config.get("storage-initializer-image", "").strip()
        ):
            raise ErrorWithStatus(
                "Missing required config: storage-initializer-image", BlockedStatus
            )

    def _validate_workload_config(self) -> None:
        """Validate the accelerator, worker resources and vLLM options."""
        if self._accelerator not in ACCELERATOR_DEFAULTS:
            raise ErrorWithStatus(
                f"accelerator must be one of: {', '.join(ACCELERATOR_DEFAULTS)}", BlockedStatus
            )
        if self._is_gpu and self._gpu_count < 1:
            raise ErrorWithStatus("gpu-count must be >= 1", BlockedStatus)
        if self.model.config["max-model-len"] < 0:
            raise ErrorWithStatus("max-model-len must be >= 0", BlockedStatus)
        if not 0 <= self.model.config["gpu-memory-utilization"] <= 1:
            raise ErrorWithStatus("gpu-memory-utilization must be in (0, 1]", BlockedStatus)
        self._validate_worker_resources()
        self._vllm_extra_args()

    def _validate_worker_resources(self) -> None:
        """Validate resource quantities are positive and requests do not exceed limits."""
        resources = self._worker_resources
        quantities = {}
        for option, value in resources.items():
            try:
                quantities[option] = parse_quantity(value)
            except ValueError:
                raise ErrorWithStatus(f"Invalid {option}: {value}", BlockedStatus)
            if quantities[option] <= 0:
                raise ErrorWithStatus(f"{option} must be greater than 0", BlockedStatus)
        for resource in ("cpu", "memory"):
            request, limit = f"{resource}-request", f"{resource}-limit"
            if quantities[request] > quantities[limit]:
                raise ErrorWithStatus(
                    f"{request} ({resources[request]}) exceeds {limit} ({resources[limit]})",
                    BlockedStatus,
                )

    def _validate_cluster_capacity(self) -> None:
        """Block when no node could ever schedule the workers, e.g. no GPUs in the cluster."""
        resources = self._worker_resources
        request = WorkerRequest(
            cpu=resources["cpu-request"],
            memory=resources["memory-request"],
            gpus=self._gpu_count,
            workers=2 if self._enable_prefill_decode else 1,
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
        if self._uri_scheme() != "hf":
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
        # Ignore secret-changed notifications for secrets we do not consume.
        if isinstance(event, SecretChangedEvent) and (
            not self._hf_token_secret_id or event.secret.id != self._hf_token_secret_id
        ):
            return
        try:
            self._validate_llmisvc_relation()
            self._validate_config()
            self._validate_workload_config()
            self._validate_hf_token()
            self._validate_s3()
            self._validate_cluster_capacity()

            self.unit.status = MaintenanceStatus("Applying LLMInferenceService")
            self.resource_handler.context = self._context
            self.resource_handler.apply()

            # Prune the HF token Secret when the token is no longer in use (e.g.
            # the config was cleared) since apply() does not remove it.
            if not self._use_hf_token():
                self._delete_resource(
                    self.resource_handler.lightkube_client, Secret, self._hf_secret_name
                )

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
