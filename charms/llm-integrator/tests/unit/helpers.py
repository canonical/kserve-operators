# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Small utility helpers for llm-integrator unit tests."""

from typing import Iterable, Optional, Type

from lightkube.models.core_v1 import NodeSpec, NodeStatus, Taint
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Node
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
