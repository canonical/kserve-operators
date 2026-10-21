# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tell a starting LLMInferenceService workload apart from a failing one.

KServe reports the same ``MinimumReplicasUnavailable`` condition while the model
is still downloading or loading, when a pod is broken, and while extra replicas
start next to ones that already serve, so the workload pods are inspected to
decide between them.
"""

from dataclasses import dataclass
from typing import Collection, Iterable, List, Optional

from lightkube.models.core_v1 import ContainerStatus
from lightkube.resources.core_v1 import Pod

STORAGE_INITIALIZER_CONTAINER = "storage-initializer"
MAIN_CONTAINER = "main"
# Worker role KServe puts on workload pods: "both", or "decode" / "prefill".
ROLE_LABEL = "llm-d.ai/role"
# Roles that must each have a ready worker when prefill/decode disaggregation is enabled.
PREFILL_DECODE_ROLES = frozenset({"decode", "prefill"})

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


def _required_roles(pods: List[Pod], expected_roles: Optional[Collection[str]]) -> set:
    """The roles that need a ready worker: the expected ones, else those of existing pods."""
    return set(expected_roles) if expected_roles else {_role(pod) for pod in pods}


def _serves_every_role(pods: List[Pod], expected_roles: Optional[Collection[str]]) -> bool:
    """True when each required worker role has at least one ready pod."""
    ready_roles = {_role(pod) for pod in pods if _is_ready(pod)}
    return _required_roles(pods, expected_roles) <= ready_roles


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


def diagnose_workload(
    pods: Iterable[Pod], expected_roles: Optional[Collection[str]] = None
) -> WorkloadState:
    """Return a failure if any workload container is broken, otherwise whether it serves.

    ``expected_roles`` lists the worker roles the configuration deploys (e.g. decode and
    prefill); each needs a ready pod to serve. Without it, the roles of the existing pods
    are used.
    """
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
    if _serves_every_role(pods, expected_roles):
        message = f"{len(pods) - len(not_ready)}/{len(pods)} workers ready"
        if not_ready:
            message += f", {_progress(not_ready[0])}"
        return WorkloadState(failed=False, message=message, serving=True)
    missing_roles = _required_roles(pods, expected_roles) - {_role(pod) for pod in pods}
    if missing_roles:
        roles = ", ".join(sorted(missing_roles))
        return WorkloadState(
            failed=False, message=f"waiting for the {roles} workers to be created"
        )
    return WorkloadState(failed=False, message=_progress(not_ready[0]))
