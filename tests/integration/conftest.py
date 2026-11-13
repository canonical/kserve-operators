#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
import shutil
from pathlib import Path

import jubilant
import pytest

WAIT_TIMEOUT = 40 * 60

logger = logging.getLogger(__name__)


class _JubilantWaitFilter(logging.Filter):
    """Keep only WARNING and above of the status diffs logged by ``Juju.wait``."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or not str(record.msg).startswith("wait:")


# jubilant 0.4 logs the wait status diffs on the "jubilant" logger itself (newer
# versions use "jubilant.wait"), so filter them there to keep the cli logs.
logging.getLogger("jubilant").addFilter(_JubilantWaitFilter())
logging.getLogger("jubilant.wait").setLevel(logging.WARNING)


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
    config.addinivalue_line(
        "markers",
        "gpu: test requires GPU hardware; skipped unless --run-gpu-tests is passed",
    )
    config.addinivalue_line(
        "markers",
        "multi_gpu: test requires GPUs on several nodes connected via InfiniBand; "
        "skipped unless --run-multi-gpu-tests is passed",
    )
    config.addinivalue_line(
        "markers",
        "cpu_only: CPU-only test (already covered by CPU CI or assumes no GPUs); "
        "skipped when --run-gpu-tests or --run-multi-gpu-tests is passed",
    )
    config.addinivalue_line(
        "markers",
        "cos: observability test that stands up cos-lite; run via the cos-integration tox env",
    )
    # Configure logging to display INFO level logs on CLI
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


def pytest_collection_modifyitems(config, items):
    run_gpu = config.getoption("--run-gpu-tests")
    run_multi_gpu = config.getoption("--run-multi-gpu-tests")
    skips = {}
    if not run_gpu:
        skips["gpu"] = pytest.mark.skip(
            reason="GPU tests are skipped by default; pass --run-gpu-tests to enable"
        )
    if not run_multi_gpu:
        skips["multi_gpu"] = pytest.mark.skip(
            reason="multi-GPU tests are skipped by default; pass --run-multi-gpu-tests to enable"
        )
    if run_gpu or run_multi_gpu:
        skips["cpu_only"] = pytest.mark.skip(
            reason="CPU-only test; skipped with --run-gpu-tests or --run-multi-gpu-tests"
        )
    for item in items:
        for marker, skip in skips.items():
            if marker in item.keywords:
                item.add_marker(skip)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()

    if report.when == "call" and report.failed:
        if item.get_closest_marker("abort_on_fail"):
            logger.warning("[ABORT] Test '%s' failed. Aborting session.", item.name)
            item.session.shouldstop = True


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
            # Match CI parity: fail fast on hook errors instead of silently retrying.
            juju_instance.cli("model-config", "automatically-retry-hooks=false")
            juju_instance.wait_timeout = WAIT_TIMEOUT
            yield juju_instance


@pytest.fixture(scope="session")
def charms_path(request: pytest.FixtureRequest) -> str:
    """Directory holding the locally built charms, from --charms-path."""
    path = request.config.getoption("--charms-path")
    if not path:
        raise ValueError("--charms-path is required for integration tests")
    return path


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
        "--charms-path",
        help="Path to directory where built charm files are stored.",
    )
    parser.addoption(
        "--run-gpu-tests",
        action="store_true",
        default=False,
        help="run tests marked 'gpu', which require GPU hardware",
    )
    parser.addoption(
        "--run-multi-gpu-tests",
        action="store_true",
        default=False,
        help="run tests marked 'multi_gpu', which require GPUs on several nodes "
        "connected via InfiniBand",
    )
