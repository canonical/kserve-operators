#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helpers for querying a deployed COS (cos-lite) stack in the observability test.

COS is reached through Traefik's proxied endpoints; Prometheus, Grafana and Loki
each sit behind a path prefix derived from the Juju model name (e.g.
``<model>-prometheus-0``). Grafana additionally needs the admin password from its
``get-admin-password`` action.
"""

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Optional, cast

import jubilant
import requests
import yaml

from .constants import NAMESPACE_DEFAULT
from .k8s import get_client

logger = logging.getLogger(__name__)

TRAEFIK_APP = "traefik"
GRAFANA_APP = "grafana"
PROMETHEUS_APP = "prometheus"
LOKI_APP = "loki"


def bundled_alert_names(rules_dir: Path) -> tuple[str, ...]:
    """Return the alert names declared in the ``*.rules`` files under ``rules_dir``."""
    names: set[str] = set()
    for rules_file in rules_dir.glob("*.rules"):
        spec = yaml.safe_load(rules_file.read_text()) or {}
        for group in spec.get("groups", []):
            for rule in group.get("rules", []):
                if "alert" in rule:
                    names.add(rule["alert"])
    assert names, f"no alert rules found under {rules_dir}"
    return tuple(sorted(names))


def bundled_dashboard_titles(dashboards_dir: Path) -> tuple[str, ...]:
    """Return the top-level titles of the ``*.tmpl`` dashboards under ``dashboards_dir``."""
    titles: set[str] = set()
    for dashboard in dashboards_dir.glob("*.tmpl"):
        title = json.loads(dashboard.read_text()).get("title")
        if title:
            titles.add(title)
    assert titles, f"no dashboards found under {dashboards_dir}"
    return tuple(sorted(titles))


def get_cos_address(juju: jubilant.Juju) -> str:
    """Return the base URL Traefik proxies the COS services on."""
    task = juju.run(f"{TRAEFIK_APP}/0", "show-proxied-endpoints")
    assert task.return_code == 0, "show-proxied-endpoints action failed"
    return json.loads(task.results["proxied-endpoints"])[TRAEFIK_APP]["url"]


def get_grafana_access(juju: jubilant.Juju) -> tuple[str, str]:
    """Return the Grafana proxied URL and admin password.

    The URL is built from Traefik's base URL (matching the other COS helpers)
    rather than the action result, since only ``admin-password`` is guaranteed.
    """
    task = juju.run(f"{GRAFANA_APP}/0", "get-admin-password")
    assert task.return_code == 0, "get-admin-password action failed"
    url = f"{get_cos_address(juju)}/{cast(str, juju.model)}-{GRAFANA_APP}"
    return url, task.results["admin-password"]


def _host(url: str) -> str:
    """Strip a leading scheme so the value can be reused in an f-string URL."""
    return url.split("//", 1)[1] if "//" in url else url


def published_prometheus_alerts(juju: jubilant.Juju, host: str) -> dict:
    """Return the alert rules Prometheus has loaded (``/api/v1/rules``)."""
    url = f"http://{_host(host)}/{cast(str, juju.model)}-prometheus-0/api/v1/rules"
    try:
        response = requests.get(url, timeout=30)
    except requests.exceptions.RequestException:
        return {}
    return response.json() if response.status_code == 200 else {}


def query_prometheus(juju: jubilant.Juju, host: str, query: str) -> Optional[dict]:
    """Run an instant Prometheus query (``/api/v1/query``)."""
    url = f"http://{_host(host)}/{cast(str, juju.model)}-prometheus-0/api/v1/query"
    try:
        response = requests.get(url, params={"query": query}, timeout=30)
    except requests.exceptions.RequestException:
        return None
    return response.json() if response.status_code == 200 else None


def published_grafana_dashboards(juju: jubilant.Juju) -> Optional[list]:
    """Return the list of dashboards registered in Grafana."""
    base_url, password = get_grafana_access(juju)
    try:
        session = requests.Session()
        session.auth = ("admin", password)
        response = session.get(f"{base_url}/api/search?query=&starred=false", timeout=30)
    except requests.exceptions.RequestException:
        return None
    return response.json() if response.status_code == 200 else None


def published_loki_logs(
    juju: jubilant.Juju, field: str, value: str, limit: int = 300
) -> Optional[dict]:
    """Return Loki logs matching ``{field=~"value"}`` (``query_range``)."""
    base_url = get_cos_address(juju)
    url = f"{base_url}/{cast(str, juju.model)}-loki-0/loki/api/v1/query_range"
    params: dict[str, str | int] = {"query": f'{{{field}=~"{value}"}}', "limit": limit}
    try:
        response = requests.get(url, params=params, timeout=30)
    except requests.exceptions.RequestException:
        return None
    return response.json() if response.status_code == 200 else None


def alert_present(alerts: dict, name: str) -> bool:
    """Return True if ``name`` appears among the loaded Prometheus alert rules."""
    return any(
        rule.get("name") == name
        for group in alerts.get("data", {}).get("groups", [])
        for rule in group.get("rules", [])
    )


def metric_has_samples(result: Optional[dict]) -> bool:
    """Return True if a Prometheus instant query returned at least one sample."""
    return bool(result and result.get("data", {}).get("result"))


def dashboard_present(dashboards: Optional[list], title: str) -> bool:
    """Return True if a dashboard with the given title is registered in Grafana."""
    return bool(dashboards) and any(board.get("title") == title for board in dashboards)


def _workload_pod_name(isvc_name: str, namespace: str = NAMESPACE_DEFAULT) -> str:
    """Return the name of a running vLLM workload pod for the given isvc."""
    from lightkube.resources.core_v1 import Pod

    pods = get_client().list(
        Pod,
        namespace=namespace,
        labels={
            "app.kubernetes.io/name": isvc_name,
            "kserve.io/component": "workload",
        },
    )
    for pod in pods:
        if (pod.status and pod.status.phase) == "Running":
            return cast(str, pod.metadata.name)
    raise AssertionError(f"No running workload pod found for LLMInferenceService '{isvc_name}'")


def generate_inference_traffic(
    isvc_name: str,
    model_name: str,
    namespace: str = NAMESPACE_DEFAULT,
    requests_count: int = 20,
    container: str = "main",
) -> None:
    """Send a small burst of completions to the workload so request metrics/logs appear.

    Runs the requests *inside* the vLLM container (``kubectl exec`` -> localhost:8000),
    which avoids a port-forward readiness race and needs no gateway route.
    """
    pod = _workload_pod_name(isvc_name, namespace)
    logger.info("Generating %d inference requests inside pod %s", requests_count, pod)

    # Script reads MODEL/COUNT from env to avoid brace-escaping in an f-string.
    script = (
        "import json, os, urllib.request\n"
        "model = os.environ['MODEL']; count = int(os.environ['COUNT'])\n"
        "ok = 0\n"
        "for i in range(count):\n"
        "    body = json.dumps({'model': model, 'prompt': 'Observability probe %d' % i,\n"
        "                       'max_tokens': 16, 'temperature': 0.7}).encode()\n"
        "    req = urllib.request.Request('http://localhost:8000/v1/completions', data=body,\n"
        "                                 headers={'Content-Type': 'application/json'})\n"
        "    try:\n"
        "        urllib.request.urlopen(req, timeout=60).read(); ok += 1\n"
        "    except Exception as e:\n"
        "        print('err', i, e)\n"
        "print('SENT', ok)\n"
    )
    cmd = [
        "kubectl",
        "exec",
        "-i",
        "-n",
        namespace,
        pod,
        "-c",
        container,
        "--",
        "env",
        f"MODEL={model_name}",
        f"COUNT={requests_count}",
        "python3",
        "-",
    ]
    result = subprocess.run(cmd, input=script.encode(), capture_output=True, timeout=600)
    out = result.stdout.decode()
    match = re.search(r"SENT (\d+)", out)
    sent = int(match.group(1)) if match else 0
    logger.info("Inference traffic done: %d/%d succeeded", sent, requests_count)
    assert sent > 0, (
        "No inference requests succeeded inside the workload pod; "
        f"stdout={out!r} stderr={result.stderr.decode()!r}"
    )
