# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Small utility helpers for llm-integrator unit tests."""

from typing import Iterable, Optional, Type

from lightkube.models.core_v1 import (
    ContainerState,
    ContainerStateRunning,
    ContainerStateTerminated,
    ContainerStateWaiting,
    ContainerStatus,
    NodeSpec,
    NodeStatus,
    PodCondition,
    PodStatus,
    Taint,
)
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Node, Pod
from ops.model import StatusBase
from ops.testing import State


def make_node(
    cpu: str = "8",
    memory: str = "32Gi",
    gpus: int = 0,
    taint_effects: Iterable[str] = (),
    unschedulable: bool = False,
) -> Node:
    """Build a Node with the given allocatable resources and scheduling constraints."""
    allocatable = {"cpu": cpu, "memory": memory}
    if gpus:
        allocatable["nvidia.com/gpu"] = str(gpus)
    return Node(
        metadata=ObjectMeta(name="node"),
        spec=NodeSpec(
            taints=[Taint(effect=effect, key="example") for effect in taint_effects],
            unschedulable=unschedulable,
        ),
        status=NodeStatus(allocatable=allocatable),
    )


def container_status(
    name: str,
    running: bool = False,
    waiting_reason: Optional[str] = None,
    restarts: int = 0,
    last_exit_code: Optional[int] = None,
    last_reason: Optional[str] = None,
) -> ContainerStatus:
    """Build a ContainerStatus in the given state."""
    state = ContainerState(
        running=ContainerStateRunning() if running else None,
        waiting=ContainerStateWaiting(reason=waiting_reason) if waiting_reason else None,
    )
    last_state = None
    if last_exit_code is not None:
        last_state = ContainerState(
            terminated=ContainerStateTerminated(exitCode=last_exit_code, reason=last_reason)
        )
    return ContainerStatus(
        name=name,
        image="image",
        imageID="image-id",
        ready=False,
        restartCount=restarts,
        state=state,
        lastState=last_state,
    )


def make_pod(
    name: str = "llm-integrator-kserve-abc",
    ready: bool = False,
    init_containers: Iterable[ContainerStatus] = (),
    containers: Iterable[ContainerStatus] = (),
    unschedulable_message: Optional[str] = None,
) -> Pod:
    """Build a workload Pod with the given container statuses and conditions."""
    conditions = [PodCondition(type="Ready", status="True" if ready else "False")]
    if unschedulable_message:
        conditions.append(
            PodCondition(type="PodScheduled", status="False", message=unschedulable_message)
        )
    return Pod(
        metadata=ObjectMeta(name=name),
        status=PodStatus(
            conditions=conditions,
            initContainerStatuses=list(init_containers),
            containerStatuses=list(containers),
        ),
    )


def assert_status(state: State, status_cls: Type[StatusBase], msg_substr: Optional[str] = None):
    """Assert ``state.unit_status`` is of ``status_cls`` and contains ``msg_substr``."""
    assert isinstance(state.unit_status, status_cls), (
        f"Expected {status_cls.__name__}, got {type(state.unit_status).__name__}: "
        f"{state.unit_status}"
    )
    if msg_substr is not None:
        assert msg_substr in state.unit_status.message, (
            f"Expected substring {msg_substr!r} in status message, "
            f"got {state.unit_status.message!r}"
        )
