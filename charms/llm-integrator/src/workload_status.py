# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tell a starting LLMInferenceService workload apart from a failing one.

KServe reports the same ``MinimumReplicasUnavailable`` condition while the model
is still downloading or loading and when a pod is broken, so the workload pods
are inspected to decide between the two.
"""

from dataclasses import dataclass
from typing import Iterable, Optional

from lightkube.models.core_v1 import ContainerStatus
from lightkube.resources.core_v1 import Pod

STORAGE_INITIALIZER_CONTAINER = "storage-initializer"
MAIN_CONTAINER = "main"

# Container waiting reasons that will not resolve without user action.
_FAILED_WAITING_REASONS = {
    "CreateContainerConfigError",
    "CreateContainerError",
    "ErrImagePull",
    "ImagePullBackOff",
    "InvalidImageName",
}


@dataclass(frozen=True)
class WorkloadState:
    """Whether the workload has failed, and a short human-readable explanation."""

    failed: bool
    message: str


def _container_failure(status: ContainerStatus) -> Optional[str]:
    waiting = status.state.waiting if status.state else None
    if waiting and waiting.reason in _FAILED_WAITING_REASONS:
        return f"{status.name}: {waiting.reason}"
    last_exit = status.lastState.terminated if status.lastState else None
    if status.restartCount and last_exit and last_exit.exitCode:
        reason = last_exit.reason or "Error"
        return f"{status.name} keeps failing ({reason}, exit code {last_exit.exitCode})"
    return None


def _is_running(container: ContainerStatus) -> bool:
    return bool(container.state and container.state.running)


def _is_ready(pod: Pod) -> bool:
    conditions = pod.status.conditions if pod.status else None
    return any(c.type == "Ready" and c.status == "True" for c in conditions or [])


def _progress(pod: Pod) -> str:
    """Describe which startup phase a not-yet-ready pod is in."""
    status = pod.status
    if status is None:
        return "starting the workload pods"
    for condition in status.conditions or []:
        if condition.type == "PodScheduled" and condition.status == "False":
            return f"waiting for resources to schedule the pod ({condition.message})"
    for container in status.initContainerStatuses or []:
        if container.name == STORAGE_INITIALIZER_CONTAINER and _is_running(container):
            return "downloading the model"
    for container in status.containerStatuses or []:
        if container.name == MAIN_CONTAINER and _is_running(container):
            return "loading the model"
    return "starting the workload pods"


def diagnose_workload(pods: Iterable[Pod]) -> WorkloadState:
    """Return a failure if any workload container is broken, otherwise the startup progress."""
    pods = list(pods)
    if not pods:
        return WorkloadState(failed=False, message="waiting for the workload pods to be created")

    for pod in pods:
        status = pod.status
        if status is None:
            continue
        containers = [*(status.initContainerStatuses or []), *(status.containerStatuses or [])]
        for container in containers:
            if failure := _container_failure(container):
                return WorkloadState(failed=True, message=f"pod {pod.metadata.name}: {failure}")

    pending = next((pod for pod in pods if not _is_ready(pod)), pods[0])
    return WorkloadState(failed=False, message=_progress(pending))
