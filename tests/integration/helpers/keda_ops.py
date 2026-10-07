#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""KEDA ScaledObject + scaling assertions for the keda bundle integration test."""

import logging
import subprocess
from contextlib import contextmanager
from typing import Iterator

from lightkube.core.exceptions import ApiError
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.resources.apiregistration_v1 import APIService
from lightkube.resources.apps_v1 import Deployment

from .constants import NAMESPACE_DEFAULT
from .k8s import get_client, get_running_workload_pod
from .retry import RETRY_FOR_TEN_MINUTES, RETRY_FOR_THREE_MINUTES

logger = logging.getLogger(__name__)

EXTERNAL_METRICS_APISERVICE = "v1beta1.external.metrics.k8s.io"

ScaledObject = create_namespaced_resource(
    group="keda.sh", version="v1alpha1", kind="ScaledObject", plural="scaledobjects"
)


def wait_for_scaled_object(name: str, namespace: str):
    """Block until the named ScaledObject exists and return it."""
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            return get_client().get(ScaledObject, name=name, namespace=namespace)


def assert_scaled_object_ready(name: str, namespace: str) -> None:
    """Block until KEDA reports the ScaledObject Ready, i.e. its metric query works."""
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            scaled_object = get_client().get(ScaledObject, name=name, namespace=namespace)
            conditions = (scaled_object.status or {}).get("conditions", [])
            assert any(
                c.get("type") == "Ready" and c.get("status") == "True" for c in conditions
            ), f"ScaledObject {name} not Ready: {conditions}"


def assert_scaled_object_absent(name: str, namespace: str) -> None:
    """Block until the named ScaledObject no longer exists."""
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            try:
                get_client().get(ScaledObject, name=name, namespace=namespace)
            except ApiError as err:
                if err.status.code == 404:
                    return
                raise
            raise AssertionError(f"ScaledObject {name} still exists")


# Concurrent completions loop run inside the vLLM container to keep
# ``vllm:num_requests_running`` above the KEDA trigger threshold.
_LOAD_SCRIPT = (
    "import json, os, urllib.request, concurrent.futures as cf\n"
    "model = os.environ['MODEL']\n"
    "def call(i):\n"
    "    body = json.dumps({'model': model, 'prompt': 'keda load %d' % i,\n"
    "                       'max_tokens': 128, 'temperature': 0.9}).encode()\n"
    "    req = urllib.request.Request('http://localhost:8000/v1/completions', data=body,\n"
    "                                 headers={'Content-Type': 'application/json'})\n"
    "    try:\n"
    "        urllib.request.urlopen(req, timeout=120).read()\n"
    "    except Exception:\n"
    "        pass\n"
    "with cf.ThreadPoolExecutor(max_workers=16) as ex:\n"
    "    list(ex.map(call, range(100000)))\n"
)

# Kills the in-pod load client by scanning /proc, since ``kubectl exec -i`` has no
# TTY and terminating the local process does not stop the remote one.
_KILL_LOAD = (
    'for d in /proc/[0-9]*; do c=$(tr "\\0" " " < "$d/cmdline" 2>/dev/null); '
    'case "$c" in "python3 - "*) kill -9 "${d#/proc/}" 2>/dev/null;; esac; done'
)


@contextmanager
def sustained_workload_load(
    isvc_name: str, model_name: str, namespace: str = NAMESPACE_DEFAULT
) -> Iterator[None]:
    """Drive concurrent inference load inside the workload pod for the block's duration."""
    pod = get_running_workload_pod(isvc_name, namespace)
    logger.info("Starting sustained load inside %s", pod)
    process = subprocess.Popen(
        [
            "kubectl",
            "exec",
            "-i",
            "-n",
            namespace,
            pod,
            "-c",
            "main",
            "--",
            "env",
            f"MODEL={model_name}",
            "python3",
            "-",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process.stdin.write(_LOAD_SCRIPT.encode())
    process.stdin.close()
    try:
        yield
    finally:
        logger.info("Stopping sustained load inside %s", pod)
        subprocess.run(
            ["kubectl", "exec", "-n", namespace, pod, "-c", "main", "--", "sh", "-c", _KILL_LOAD],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()


def assert_deployment_scaled_to(name: str, namespace: str, replicas: int) -> None:
    """Block until the Deployment's desired replica count is ``replicas``.

    Asserts on ``.spec.replicas`` (the value KEDA's generated HPA sets) rather than
    ready replicas, so the check reflects KEDA's scaling decision and does not hinge
    on another heavy vLLM pod scheduling and becoming Ready on a constrained runner.
    """
    client = get_client()
    for attempt in RETRY_FOR_TEN_MINUTES:
        with attempt:
            deployment = client.get(Deployment, name=name, namespace=namespace)
            desired = deployment.spec.replicas
            assert (
                desired == replicas
            ), f"Deployment {name} desired replicas {desired}, want {replicas}"


def assert_external_metrics_apiservice_available() -> None:
    """Block until KEDA's external-metrics APIService reports Available=True."""
    client = get_client()
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            api = client.get(APIService, name=EXTERNAL_METRICS_APISERVICE)
            conditions = api.status.conditions if api.status else []
            available = any(
                getattr(c, "type", None) == "Available" and getattr(c, "status", None) == "True"
                for c in conditions or []
            )
            assert available, "external.metrics.k8s.io APIService not Available yet"


def assert_crd_absent(name: str) -> None:
    """Block until the named CRD no longer exists."""
    client = get_client()
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            try:
                client.get(CustomResourceDefinition, name=name)
            except ApiError as err:
                if err.status.code == 404:
                    return
                raise
            raise AssertionError(f"CRD {name} still exists after keda removal")
