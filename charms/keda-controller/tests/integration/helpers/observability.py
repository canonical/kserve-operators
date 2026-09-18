# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helpers for the keda observability relation integration tests.

Standalone Prometheus/Loki have no ingress, and in-cluster service DNS is not
routable from the test host, so they are reached over a short-lived
``kubectl port-forward`` and then queried over HTTP. The forward is (re)opened
inside the retried assertions so a Prometheus/Loki workload restart (e.g. when a
new relation reconfigures it) is simply retried against a fresh forward.
"""

import contextlib
import json
import logging
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Iterator, Optional, Sequence
from urllib.parse import urlparse

import tenacity

logger = logging.getLogger(__name__)

RETRY = tenacity.retry(
    stop=tenacity.stop_after_delay(300),
    wait=tenacity.wait_fixed(10),
    reraise=True,
)


def _free_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_listening(port: int, proc: "subprocess.Popen", timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"kubectl port-forward exited early: {output}")
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.5)
    raise TimeoutError(f"port-forward on 127.0.0.1:{port} never became reachable")


@contextlib.contextmanager
def port_forward(model: str, service: str, remote_port: int) -> Iterator[str]:
    """Port-forward ``svc/<service>`` in ``model`` and yield a localhost base URL."""
    local_port = _free_local_port()
    proc = subprocess.Popen(
        [
            "kubectl",
            "-n",
            model,
            "port-forward",
            f"svc/{service}",
            f"{local_port}:{remote_port}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_until_listening(local_port, proc)
        yield f"http://127.0.0.1:{local_port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _get_json(url: str, params: Optional[dict] = None, timeout: int = 30) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 (localhost)
        return json.loads(response.read().decode())


@RETRY
def assert_prometheus_scrapes_ports(
    model: str, service: str, port: int, app: str, expected_ports: Sequence[int]
) -> None:
    """Assert Prometheus has healthy scrape targets on every expected port for ``app``."""
    with port_forward(model, service, port) as url:
        data = _get_json(f"{url}/api/v1/targets")
        targets = [
            target
            for target in data["data"]["activeTargets"]
            if target["labels"].get("juju_application") == app
        ]
        assert targets, f"Prometheus has no active targets for {app} yet"

        unhealthy = [t["scrapeUrl"] for t in targets if t.get("health") != "up"]
        assert not unhealthy, f"targets not up: {unhealthy}"

        scraped_ports = {urlparse(t["scrapeUrl"]).port for t in targets}
        missing = set(expected_ports) - scraped_ports
        assert not missing, f"Prometheus is not scraping ports {missing} (have {scraped_ports})"


@RETRY
def assert_loki_has_logs(model: str, service: str, port: int, app: str) -> None:
    """Assert Loki has received log lines from ``app`` over the last hour."""
    with port_forward(model, service, port) as url:
        end = time.time_ns()
        start = end - 3600 * 1_000_000_000
        data = _get_json(
            f"{url}/loki/api/v1/query_range",
            params={
                "query": f'{{juju_application="{app}"}}',
                "start": start,
                "end": end,
                "limit": 50,
            },
        )
        assert data["data"]["result"], f"Loki has no logs for {app} yet"
