#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
import shutil
from pathlib import Path

import jubilant
import lightkube
import pytest

WAIT_TIMEOUT = 40 * 60

logger = logging.getLogger(__name__)


def _configure_juju_cli_path() -> None:
    """Prefer direct snap Juju binary to avoid snap launcher cgroup checks in tox."""
    juju_path = shutil.which("juju") or ""
    direct_snap_juju = Path("/snap/juju/current/bin/juju")

    if juju_path == "/snap/bin/juju" and direct_snap_juju.exists():
        current_path = os.environ.get("PATH", "")
        direct_dir = str(direct_snap_juju.parent)
        if direct_dir not in current_path.split(":"):
            os.environ["PATH"] = f"{direct_dir}:{current_path}"
            logger.info("Using direct Juju binary from %s", direct_dir)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "abort_on_fail: abort the entire test session if this test fails",
    )
    # Configure logging to display INFO level logs on CLI
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    # Quiet jubilant's per-poll wait logging, which is very verbose during the
    # long waits in these tests.
    logging.getLogger("jubilant.wait").setLevel("WARNING")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()

    if report.when == "call" and report.failed:
        if item.get_closest_marker("abort_on_fail"):
            logger.warning("[ABORT] Test '%s' failed. Aborting session.", item.name)
            item.session.shouldstop = True


def pytest_addoption(parser):
    parser.addoption(
        "--keep-models",
        action="store_true",
        default=False,
        help="keep temporarily-created models",
    )
    parser.addoption(
        "--model",
        action="store",
        help="Juju model to use; if not provided, a temporary model is created",
        default=None,
    )
    parser.addoption(
        "--charm-path",
        help="Path to charm file for performing tests on.",
    )


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest):
    _configure_juju_cli_path()

    keep_models = bool(request.config.getoption("--keep-models"))
    model_name = request.config.getoption("--model")

    if model_name:
        juju_instance = jubilant.Juju(model=model_name)
        juju_instance.wait_timeout = WAIT_TIMEOUT
        yield juju_instance
    else:
        with jubilant.temp_model(keep=keep_models) as juju_instance:
            juju_instance.wait_timeout = WAIT_TIMEOUT
            yield juju_instance


@pytest.fixture(scope="session")
def lightkube_client() -> lightkube.Client:
    return lightkube.Client(field_manager="lws-controller")
