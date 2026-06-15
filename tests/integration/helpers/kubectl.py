#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Thin wrappers around ``kubectl``/``helm`` invocations used by the bundle tests.

Resource operations in the bundle tests go through lightkube; the helpers that
remain here cover the few things lightkube does not do: Helm chart installs,
applying upstream multi-document install bundles served from remote URLs
(``kubectl apply -f <url>``) and port-forwarding a service for HTTP probes.
"""

import logging
import subprocess
import time
from contextlib import contextmanager, suppress
from typing import Iterator

from .command import run_command

logger = logging.getLogger(__name__)

# Helm installs pull charts/images from remote OCI registries, which can fail
# transiently (network blips, registry rate limits). Retry a few times before
# giving up.
HELM_INSTALL_ATTEMPTS = 3
HELM_RETRY_DELAY_SECONDS = 10


def kubectl(args: list[str], check: bool = True) -> str:
    """Run a ``kubectl`` command and return its stripped stdout."""
    return run_command(["kubectl", *args], check=check)


def helm(args: list[str], check: bool = True) -> str:
    """Run a ``helm`` command and return its stripped stdout.

    Retries transient failures up to ``HELM_INSTALL_ATTEMPTS`` times so flaky
    registry/network errors don't fail the whole test run.
    """
    last_error = None
    for attempt in range(1, HELM_INSTALL_ATTEMPTS + 1):
        try:
            return run_command(["helm", *args], check=check)
        except subprocess.CalledProcessError as err:
            last_error = err
            logger.warning(
                "helm command failed (attempt %s/%s), retrying: %s",
                attempt,
                HELM_INSTALL_ATTEMPTS,
                " ".join(args),
            )
            if attempt < HELM_INSTALL_ATTEMPTS:
                time.sleep(HELM_RETRY_DELAY_SECONDS)
    raise RuntimeError(
        f"helm command failed after {HELM_INSTALL_ATTEMPTS} attempts: {' '.join(args)}"
    ) from last_error


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
