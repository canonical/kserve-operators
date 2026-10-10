# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tell a starting LLMInferenceService workload apart from a failing one.

KServe reports the same ``MinimumReplicasUnavailable`` condition while the model
is still downloading or loading, when a pod is broken, and while extra replicas
start next to ones that already serve, so the workload pods are inspected to
decide between them.
"""

from dataclasses import dataclass
from typing import Iterable, List, Optional

from lightkube.models.core_v1 import ContainerStatus
from lightkube.resources.core_v1 import Pod

STORAGE_INITIALIZER_CONTAINER = "storage-initializer"
MAIN_CONTAINER = "main"
# Worker role KServe puts on workload pods: "both", or "decode" / "prefill".
ROLE_LABEL = "llm-d.ai/role"

# Container waiting reasons that will not resolve without user action.
_FAILED_WAITING_REASONS = {
    "CrashLoopBackOff",
    "CreateContainerConfigError",
    "CreateContainerError",
    "ErrImagePull",
    "ImagePullBackOff",
    "InvalidImageName",
}


@dataclass(frozen=True)
class WorkloadState:
    """Whether the workload has failed or already serves, and a short explanation."""

    failed: bool
    message: str
    serving: bool = False


def _container_failure(status: ContainerStatus) -> Optional[str]:
    waiting = status.state.waiting if status.state else None
    if not waiting or waiting.reason not in _FAILED_WAITING_REASONS:
        return None
    last_exit = status.lastState.terminated if status.lastState else None
    if waiting.reason == "CrashLoopBackOff" and last_exit:
        reason = last_exit.reason or "Error"
        return f"{status.name} keeps failing ({reason}, exit code {last_exit.exitCode})"
    return f"{status.name}: {waiting.reason}"


def _is_running(container: ContainerStatus) -> bool:
    return bool(container.state and container.state.running)


def _is_ready(pod: Pod) -> bool:
    conditions = pod.status.conditions if pod.status else None
    return any(c.type == "Ready" and c.status == "True" for c in conditions or [])


def _role(pod: Pod) -> Optional[str]:
    return (pod.metadata.labels or {}).get(ROLE_LABEL) if pod.metadata else None


def _serves_every_role(pods: List[Pod]) -> bool:
    """True when each worker role has at least one ready pod."""
    return {_role(pod) for pod in pods} == {_role(pod) for pod in pods if _is_ready(pod)}


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
    """Return a failure if any workload container is broken, otherwise whether it serves."""
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

    not_ready = [pod for pod in pods if not _is_ready(pod)]
    if _serves_every_role(pods):
        message = f"{len(pods) - len(not_ready)}/{len(pods)} workers ready"
        if not_ready:
            message += f", {_progress(not_ready[0])}"
        return WorkloadState(failed=False, message=message, serving=True)
    return WorkloadState(failed=False, message=_progress(not_ready[0]))
