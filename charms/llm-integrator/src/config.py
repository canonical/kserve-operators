# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Validated view of the llm-integrator Juju configuration.

Only checks that need nothing but the config live here; checks against relations, secrets or the
cluster stay in the charm.
"""

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from lightkube.utils.quantity import parse_quantity
from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

DEFAULT_IMAGES = json.loads((Path(__file__).parent / "default-custom-images.json").read_text())

CPU = "cpu"
NVIDIA_GPU = "nvidia-gpu"

HF_URI_PREFIX = "hf://"
S3_URI_PREFIX = "s3://"

# Worker resource options; empty values fall back to the accelerator defaults.
RESOURCE_OPTIONS = ("cpu-request", "cpu-limit", "memory-request", "memory-limit")

# autoscaling-metric presets.
NUM_REQUESTS = "num-requests"
NUM_REQUESTS_WAITING = "num-requests-waiting"
KV_CACHE_USAGE = "kv-cache-usage"
AUTOSCALING_METRICS = (NUM_REQUESTS, NUM_REQUESTS_WAITING, KV_CACHE_USAGE)

# Upper bound Kubernetes accepts for an HPA stabilization window.
MAX_SCALE_DOWN_DELAY = 3600


@dataclass(frozen=True)
class AcceleratorDefaults:
    """Defaults for an accelerator, used when the matching config option is empty."""

    image: str
    resources: Dict[str, str]
    # Concurrent requests per replica the num-requests autoscaling metric aims for.
    num_requests_target: float


# Sized for small models and override-able per model. cpu: the values the CPU bundle tests have
# always run with; nvidia-gpu: the single-GPU manifest validated with Qwen3-4B on the GPU CI
# machine, with the 4-CPU limit taken from KServe's single-node GPU sample.
ACCELERATOR_DEFAULTS = {
    CPU: AcceleratorDefaults(
        image=DEFAULT_IMAGES["vllm"],
        resources={
            "cpu-request": "500m",
            "cpu-limit": "2",
            "memory-request": "4Gi",
            "memory-limit": "8Gi",
        },
        num_requests_target=2,
    ),
    NVIDIA_GPU: AcceleratorDefaults(
        image=DEFAULT_IMAGES["vllm_gpu"],
        resources={
            "cpu-request": "2",
            "cpu-limit": "4",
            "memory-request": "8Gi",
            "memory-limit": "16Gi",
        },
        num_requests_target=16,
    ),
}

# Per-replica targets of the metrics whose default does not depend on the accelerator.
METRIC_TARGET_DEFAULTS = {NUM_REQUESTS_WAITING: 2, KV_CACHE_USAGE: 0.8}

# vLLM flags that vllm-extra-args may not set, with the reason surfaced to the user.
RESERVED_VLLM_FLAGS = {
    "--host": "the host is set by the charm and cannot be changed",
    "--port": "the port is set by the charm and cannot be changed",
    "--model": "use the charm's model-uri config option instead",
    "--served-model-name": "use the charm's model-name config option instead",
    "--tensor-parallel-size": "use the charm's gpu-count config option instead",
    "-tp": "use the charm's gpu-count config option instead",
    "--pipeline-parallel-size": "use the charm's gpu-count config option instead",
    "-pp": "use the charm's gpu-count config option instead",
    "--data-parallel-size": "use the charm's gpu-count config option instead",
    "-dp": "use the charm's gpu-count config option instead",
    "--max-model-len": "use the charm's max-model-len config option instead",
    "--gpu-memory-utilization": "use the charm's gpu-memory-utilization config option instead",
    "--kv-transfer-config": "KV-cache transfer is not supported by the charm yet",
}


def validation_error_message(error: ValidationError) -> str:
    """Turn the first validation error into a one-line status message."""
    first = error.errors()[0]
    if first["type"] == "value_error":
        return str(first["ctx"]["error"])
    option = ".".join(str(part) for part in first["loc"]).replace("_", "-")
    return f"Invalid {option}: {first['msg']}"


class CharmConfig(BaseModel):
    """The charm's Juju config options (with underscores), validated.

    Fields follow the order of config.yaml. hf-token-secret is not part of the model: it is
    resolved and checked by the charm.
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    model_uri: str
    model_name: str
    runtime_image: str
    accelerator: str
    gpu_count: int
    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str
    max_model_len: int
    gpu_memory_utilization: float
    vllm_extra_args: List[str]
    storage_initializer_image: str
    enable_prefill_decode: bool
    min_replicas: int
    max_replicas: int
    autoscaling_metric: str
    autoscaling_target: float
    autoscaling_polling_interval: int
    autoscaling_scale_down_delay: int

    @field_validator("*", mode="before")
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("model_uri")
    @classmethod
    def _check_model_uri(cls, value: str) -> str:
        if not value:
            raise ValueError("Missing required config: model-uri")
        if not value.startswith((HF_URI_PREFIX, S3_URI_PREFIX)):
            raise ValueError("model-uri must start with 'hf://' or 's3://'")
        return value

    @field_validator("accelerator")
    @classmethod
    def _check_accelerator(cls, value: str) -> str:
        if value not in ACCELERATOR_DEFAULTS:
            raise ValueError(f"accelerator must be one of: {', '.join(ACCELERATOR_DEFAULTS)}")
        return value

    @field_validator("max_model_len")
    @classmethod
    def _check_max_model_len(cls, value: int) -> int:
        if value < 0:
            raise ValueError("max-model-len must be >= 0")
        return value

    @field_validator("vllm_extra_args", mode="before")
    @classmethod
    def _parse_vllm_extra_args(cls, value: str) -> List[str]:
        try:
            args = shlex.split(value)
        except ValueError as err:
            raise ValueError(f"Invalid vllm-extra-args: {err}") from err
        for arg in args:
            # vLLM accepts --flag=value and underscores in flag names.
            flag = arg.split("=", 1)[0].replace("_", "-")
            if flag in RESERVED_VLLM_FLAGS:
                raise ValueError(
                    f"vllm-extra-args must not set {flag}: {RESERVED_VLLM_FLAGS[flag]}"
                )
        return args

    @model_validator(mode="after")
    def _check_gpu_options(self) -> "CharmConfig":
        if not self.is_gpu:
            return self
        if self.gpu_count < 1:
            raise ValueError("gpu-count must be >= 1")
        if not 0 <= self.gpu_memory_utilization <= 1:
            raise ValueError("gpu-memory-utilization must be in (0, 1], or 0 for vLLM's default")
        return self

    @model_validator(mode="after")
    def _check_worker_resources(self) -> "CharmConfig":
        resources = self.worker_resources
        quantities = {}
        for option, value in resources.items():
            try:
                quantities[option] = parse_quantity(value)
            except ValueError:
                raise ValueError(f"Invalid {option}: {value}")
            if quantities[option] <= 0:
                raise ValueError(f"{option} must be greater than 0")
        for resource in ("cpu", "memory"):
            request, limit = f"{resource}-request", f"{resource}-limit"
            if quantities[request] > quantities[limit]:
                raise ValueError(
                    f"{request} ({resources[request]}) exceeds {limit} ({resources[limit]})"
                )
        return self

    @model_validator(mode="after")
    def _check_replicas(self) -> "CharmConfig":
        if self.min_replicas < 1:
            raise ValueError("min-replicas must be >= 1")
        if self.max_replicas < self.min_replicas:
            raise ValueError(
                f"max-replicas ({self.max_replicas}) must be >= "
                f"min-replicas ({self.min_replicas})"
            )
        return self

    @model_validator(mode="after")
    def _check_autoscaling(self) -> "CharmConfig":
        if not self.autoscaling_enabled:
            return self
        if self.autoscaling_metric not in AUTOSCALING_METRICS:
            raise ValueError(
                f"autoscaling-metric must be one of: {', '.join(AUTOSCALING_METRICS)}"
            )
        if self.autoscaling_target < 0:
            raise ValueError("autoscaling-target must be > 0, or 0 for the metric's default")
        if self.autoscaling_metric == KV_CACHE_USAGE and self.autoscaling_target > 1:
            raise ValueError(
                "autoscaling-target must be in (0, 1] for kv-cache-usage, "
                "or 0 for the metric's default"
            )
        if self.autoscaling_polling_interval < 1:
            raise ValueError("autoscaling-polling-interval must be >= 1")
        if not 0 <= self.autoscaling_scale_down_delay <= MAX_SCALE_DOWN_DELAY:
            raise ValueError(
                f"autoscaling-scale-down-delay must be between 0 and {MAX_SCALE_DOWN_DELAY}"
            )
        return self

    @property
    def autoscaling_enabled(self) -> bool:
        """KEDA scales the workers between min-replicas and max-replicas."""
        return self.max_replicas > self.min_replicas

    @property
    def fixed_replicas(self) -> Optional[int]:
        """Replicas of each worker, or None when KEDA sets them."""
        return None if self.autoscaling_enabled else self.min_replicas

    @property
    def is_gpu(self) -> bool:
        return self.accelerator == NVIDIA_GPU

    @property
    def worker_gpus(self) -> int:
        """GPUs requested per worker; 0 for CPU workloads."""
        return self.gpu_count if self.is_gpu else 0

    @property
    def uri_scheme(self) -> str:
        """The model URI scheme: ``hf`` or ``s3``."""
        return "hf" if self.model_uri.startswith(HF_URI_PREFIX) else "s3"

    @property
    def served_model_name(self) -> str:
        """model-name, or the model reference derived from the URI when unset.

        hf://EleutherAI/pythia-70m -> EleutherAI/pythia-70m; s3://bucket/models/pythia-70m ->
        pythia-70m (the last path segment).
        """
        if self.model_name:
            return self.model_name
        if self.uri_scheme == "hf":
            return self.model_uri.removeprefix(HF_URI_PREFIX)
        return self.model_uri.removeprefix(S3_URI_PREFIX).rstrip("/").rsplit("/", 1)[-1]

    @property
    def effective_runtime_image(self) -> str:
        return self.runtime_image or ACCELERATOR_DEFAULTS[self.accelerator].image

    @property
    def effective_storage_initializer_image(self) -> str:
        return self.storage_initializer_image or DEFAULT_IMAGES["storage_initializer"]

    @property
    def worker_resources(self) -> Dict[str, str]:
        """Per-worker resource quantities keyed by config option name."""
        defaults = ACCELERATOR_DEFAULTS[self.accelerator].resources
        return {
            option: getattr(self, option.replace("-", "_")) or defaults[option]
            for option in RESOURCE_OPTIONS
        }

    @property
    def autoscaling_threshold(self) -> float:
        """Per-replica target of the autoscaling metric."""
        if self.autoscaling_target:
            return self.autoscaling_target
        if self.autoscaling_metric == NUM_REQUESTS:
            return ACCELERATOR_DEFAULTS[self.accelerator].num_requests_target
        return METRIC_TARGET_DEFAULTS[self.autoscaling_metric]
