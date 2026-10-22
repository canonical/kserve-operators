# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Check whether the cluster has nodes that can host the LLM worker pods.

The check compares worker resource requests against each node's allocatable
capacity, not its free capacity: resources that exist but are in use only delay
scheduling, while resources that do not exist at all need user action.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from lightkube.resources.core_v1 import Node
from lightkube.utils.quantity import parse_quantity

GPU_RESOURCE = "nvidia.com/gpu"
_BLOCKING_TAINT_EFFECTS = {"NoSchedule", "NoExecute"}


@dataclass(frozen=True)
class WorkerRequest:
    """Resources requested by each worker pod and how many workers are deployed."""

    cpu: str
    memory: str
    gpus: int
    workers: int

    def __str__(self) -> str:
        parts = [f"cpu={self.cpu}", f"memory={self.memory}"]
        if self.gpus:
            parts.append(f"{GPU_RESOURCE}={self.gpus}")
        return ", ".join(parts)


def _is_schedulable(node: Node) -> bool:
    """True when the charm's pods (which set no tolerations) can land on the node."""
    spec = node.spec
    if spec is None:
        return True
    if spec.unschedulable:
        return False
    return not any(taint.effect in _BLOCKING_TAINT_EFFECTS for taint in spec.taints or [])


def _allocatable(node: Node, resource: str) -> Decimal:
    allocatable = (node.status.allocatable if node.status else None) or {}
    value = allocatable.get(resource)
    return parse_quantity(value) if value else Decimal(0)


def _worker_slots(node: Node, request: WorkerRequest) -> int:
    """Return how many workers fit into the node's allocatable resources."""
    wanted = {"cpu": parse_quantity(request.cpu), "memory": parse_quantity(request.memory)}
    if request.gpus:
        wanted[GPU_RESOURCE] = Decimal(request.gpus)
    return min(int(_allocatable(node, name) // amount) for name, amount in wanted.items())


def find_capacity_issue(nodes: Iterable[Node], request: WorkerRequest) -> Optional[str]:
    """Return why the workers can never be scheduled, or None when they fit.

    Requests must be positive, valid Kubernetes quantities.
    """
    schedulable = [node for node in nodes if _is_schedulable(node)]
    if not schedulable:
        return "No schedulable nodes found in the cluster"

    if request.gpus:
        max_gpus = max(_allocatable(node, GPU_RESOURCE) for node in schedulable)
        if max_gpus < request.gpus:
            return (
                f"No node offers {request.gpus} {GPU_RESOURCE} (max allocatable: {max_gpus:.0f})"
            )

    slots = sum(_worker_slots(node, request) for node in schedulable)
    if slots == 0:
        return f"No node can fit a worker requesting {request}"
    if slots < request.workers:
        return f"Cluster fits {slots} of {request.workers} workers requesting {request}"
    return None
