# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for telling a starting workload apart from a failing one."""

import pytest

from workload_status import diagnose_workload

from .helpers import container_status, make_pod


@pytest.mark.parametrize(
    "pods, expected",
    [
        pytest.param([], "waiting for the workload pods to be created", id="no-pods"),
        pytest.param(
            [
                make_pod(
                    unschedulable_message="0/1 nodes are available: Insufficient nvidia.com/gpu"
                )
            ],
            "waiting for resources to schedule the pod (0/1 nodes are available",
            id="unschedulable",
        ),
        pytest.param(
            [make_pod(init_containers=[container_status("storage-initializer", running=True)])],
            "downloading the model",
            id="downloading",
        ),
        pytest.param(
            [make_pod(containers=[container_status("main", running=True)])],
            "loading the model",
            id="loading",
        ),
        pytest.param(
            [make_pod(containers=[container_status("main", waiting_reason="ContainerCreating")])],
            "starting the workload pods",
            id="creating",
        ),
        pytest.param(
            [
                make_pod(name="ready", ready=True),
                make_pod(containers=[container_status("main", running=True)]),
            ],
            "loading the model",
            id="reports-the-pod-not-ready-yet",
        ),
        pytest.param(
            [
                make_pod(
                    containers=[
                        container_status("main", running=True, restarts=1, last_exit_code=1)
                    ]
                )
            ],
            "loading the model",
            id="recovered-after-a-crash",
        ),
    ],
)
def test_starting_workload_is_not_failed(pods, expected):
    state = diagnose_workload(pods)
    assert not state.failed
    assert expected in state.message


@pytest.mark.parametrize(
    "container, expected",
    [
        pytest.param(
            container_status(
                "storage-initializer",
                waiting_reason="CrashLoopBackOff",
                restarts=3,
                last_exit_code=137,
                last_reason="OOMKilled",
            ),
            "storage-initializer keeps failing (OOMKilled, exit code 137)",
            id="oom-killed",
        ),
        pytest.param(
            container_status(
                "main", waiting_reason="CrashLoopBackOff", restarts=2, last_exit_code=1
            ),
            "main keeps failing (Error, exit code 1)",
            id="crash-loop-after-error",
        ),
        pytest.param(
            container_status("main", waiting_reason="CrashLoopBackOff", restarts=2),
            "main: CrashLoopBackOff",
            id="crash-loop-without-last-state",
        ),
        pytest.param(
            container_status("main", waiting_reason="ImagePullBackOff"),
            "main: ImagePullBackOff",
            id="image-pull",
        ),
        pytest.param(
            container_status("main", waiting_reason="CreateContainerConfigError"),
            "main: CreateContainerConfigError",
            id="config-error",
        ),
    ],
)
def test_broken_container_fails_workload(container, expected):
    pod = make_pod(name="llm-integrator-kserve-abc", containers=[container])
    state = diagnose_workload([pod])
    assert state.failed
    assert state.message == f"pod llm-integrator-kserve-abc: {expected}"


def test_clean_restart_is_not_a_failure():
    container = container_status("main", running=True, restarts=1, last_exit_code=0)
    assert not diagnose_workload([make_pod(containers=[container])]).failed
