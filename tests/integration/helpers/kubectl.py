#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""kubectl helpers for the bundle tests.

Resource operations in the bundle tests go through lightkube; kubectl is only
used for what lightkube does not do: port-forwarding a service for HTTP probes
and attaching to a short-lived pod to read files from a node.
"""

import json
import logging
import subprocess
import uuid
from contextlib import contextmanager, suppress
from typing import Iterator

logger = logging.getLogger(__name__)


@contextmanager
def port_forward(namespace: str, target: str, *port_mappings: str) -> Iterator[None]:
    """Run ``kubectl port-forward`` for the duration of the ``with`` block.

    The forwarding process is always terminated and reaped on exit, even if the
    body raises.
    """
    process = subprocess.Popen(
        ["kubectl", "-n", namespace, "port-forward", target, *port_mappings],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield
    finally:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            # terminate() didn't take; escalate to SIGKILL so we don't leak a
            # background port-forward that could interfere with later tests.
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(subprocess.TimeoutExpired, ProcessLookupError):
                process.wait(timeout=20)
        except ProcessLookupError:
            pass


def read_node_files(node: str, image: str, *paths: str) -> list[str]:
    """Return the content of the single-line files ``paths`` on ``node``.

    Runs ``cat`` in a short-lived pod pinned to the node. The pod uses the host
    network so that its ``/sys`` shows the host network devices (e.g. the
    InfiniBand HCAs); pick an ``image`` already present on the node.
    """
    overrides = {"spec": {"nodeName": node, "hostNetwork": True}}
    result = subprocess.run(
        [
            "kubectl",
            "run",
            f"read-node-files-{uuid.uuid4().hex[:8]}",
            "--quiet",
            "--rm",
            "--stdin",
            "--restart=Never",
            f"--image={image}",
            f"--overrides={json.dumps(overrides)}",
            "--command",
            "--",
            "cat",
            *paths,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
        stdin=subprocess.DEVNULL,
    )
    lines = result.stdout.splitlines()
    if len(lines) != len(paths):
        raise AssertionError(
            f"Reading {paths} on node {node} returned {result.stdout!r} {result.stderr!r}"
        )
    return [line.strip() for line in lines]
