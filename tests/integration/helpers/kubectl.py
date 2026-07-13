#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helper for port-forwarding a Kubernetes service during the bundle tests.

Resource operations in the bundle tests go through lightkube; the only thing
that still needs a subprocess is port-forwarding a service for HTTP probes,
which lightkube does not do.
"""

import logging
import subprocess
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
