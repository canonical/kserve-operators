# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the cluster capacity check."""

import pytest

from capacity import WorkerRequest, find_capacity_issue

from .helpers import make_node

CPU_WORKER = WorkerRequest(cpu="500m", memory="4Gi", gpus=0, workers=1)
GPU_WORKER = WorkerRequest(cpu="2", memory="8Gi", gpus=1, workers=1)
GPU_PREFILL_DECODE = WorkerRequest(cpu="2", memory="8Gi", gpus=1, workers=2)


@pytest.mark.parametrize(
    "nodes, request_, expected",
    [
        pytest.param([make_node()], CPU_WORKER, None, id="cpu-fits"),
        pytest.param([make_node(gpus=1)], GPU_WORKER, None, id="gpu-fits"),
        pytest.param(
            [make_node(gpus=1, taint_effects=["PreferNoSchedule"])],
            GPU_WORKER,
            None,
            id="soft-taint-ignored",
        ),
        pytest.param([make_node(gpus=2)], GPU_PREFILL_DECODE, None, id="pd-one-node"),
        pytest.param(
            [make_node(gpus=1), make_node(gpus=1)], GPU_PREFILL_DECODE, None, id="pd-two-nodes"
        ),
        pytest.param([], CPU_WORKER, "No schedulable nodes", id="no-nodes"),
        pytest.param(
            [make_node(unschedulable=True)], CPU_WORKER, "No schedulable nodes", id="cordoned"
        ),
        pytest.param(
            [make_node(gpus=8, taint_effects=["NoSchedule"])],
            GPU_WORKER,
            "No schedulable nodes",
            id="tainted",
        ),
        pytest.param(
            [make_node()],
            GPU_WORKER,
            "No node offers 1 nvidia.com/gpu (max allocatable: 0)",
            id="no-gpus",
        ),
        pytest.param(
            [make_node(gpus=1)],
            WorkerRequest(cpu="2", memory="8Gi", gpus=2, workers=1),
            "No node offers 2 nvidia.com/gpu (max allocatable: 1)",
            id="too-few-gpus-per-node",
        ),
        pytest.param(
            [make_node(memory="16Gi", gpus=1), make_node(memory="64Gi")],
            WorkerRequest(cpu="2", memory="32Gi", gpus=1, workers=1),
            "No node can fit a worker requesting cpu=2, memory=32Gi, nvidia.com/gpu=1",
            id="gpu-and-memory-on-different-nodes",
        ),
        pytest.param(
            [make_node(gpus=1)],
            GPU_PREFILL_DECODE,
            "Cluster fits 1 of 2 workers requesting cpu=2, memory=8Gi, nvidia.com/gpu=1",
            id="pd-not-enough-gpus",
        ),
        pytest.param(
            [make_node(memory="6Gi")],
            WorkerRequest(cpu="500m", memory="4Gi", gpus=0, workers=2),
            "Cluster fits 1 of 2 workers requesting cpu=500m, memory=4Gi",
            id="pd-not-enough-memory",
        ),
    ],
)
def test_find_capacity_issue(nodes, request_, expected):
    issue = find_capacity_issue(nodes, request_)
    if expected is None:
        assert issue is None
    else:
        assert issue is not None and expected in issue
