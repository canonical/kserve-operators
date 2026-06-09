#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import subprocess
from contextlib import suppress

import requests

from .command import run_command
from .retry import (
    RETRY_FOR_TEN_MINUTES,
    RETRY_FOR_THREE_MINUTES,
    RETRY_FOR_TWENTY_MINUTES,
)

logger = logging.getLogger(__name__)

LLMISVC_APP_NAME = "kserve-llmisvc"
LLMISVC_CONTROLLER_METRICS_PORT = 8080
LLMISVC_AGGREGATED_METRICS_PORT = 15090
LOCAL_CONTROLLER_METRICS_PORT = 28080
LOCAL_AGGREGATED_METRICS_PORT = 25090


def install_gateway_api_crds(version: str):
    logger.info("Installing Gateway API CRDs (version %s)...", version)
    run_command(
        [
            "kubectl",
            "apply",
            "-f",
            (
                "https://github.com/kubernetes-sigs/gateway-api/releases/download/"
                f"{version}/standard-install.yaml"
            ),
        ]
    )
    logger.info("Waiting for GatewayClass CRD to be established...")
    run_command(
        [
            "kubectl",
            "wait",
            "--for=condition=Established",
            "crd/gatewayclasses.gateway.networking.k8s.io",
            "--timeout=120s",
        ]
    )
    logger.info("Gateway API CRDs installed")


def install_gie_crds(version: str):
    logger.info("Installing Gateway API Inference Extension CRDs (version %s)...", version)
    run_command(
        [
            "kubectl",
            "apply",
            "-f",
            (
                "https://github.com/kubernetes-sigs/gateway-api-inference-extension/"
                f"releases/download/{version}/manifests.yaml"
            ),
        ]
    )
    run_command(
        [
            "kubectl",
            "wait",
            "--for=condition=Established",
            "crd/inferencepools.inference.networking.x-k8s.io",
            "--timeout=120s",
        ]
    )
    logger.info("Gateway API Inference Extension CRDs installed")


def install_envoy_gateway(envoy_gateway_version: str, envoy_ai_gateway_version: str):
    logger.info("Installing Envoy Gateway (version %s)...", envoy_gateway_version)
    run_command(
        [
            "helm",
            "upgrade",
            "-i",
            "eg",
            "oci://docker.io/envoyproxy/gateway-helm",
            "--version",
            envoy_gateway_version,
            "--namespace",
            "envoy-gateway-system",
            "--create-namespace",
            "-f",
            (
                "https://raw.githubusercontent.com/envoyproxy/ai-gateway/"
                f"{envoy_ai_gateway_version}/manifests/envoy-gateway-values.yaml"
            ),
            "-f",
            (
                "https://raw.githubusercontent.com/envoyproxy/ai-gateway/"
                f"{envoy_ai_gateway_version}/examples/inference-pool/"
                "envoy-gateway-values-addon.yaml"
            ),
            "--wait",
            "--timeout",
            "300s",
        ]
    )
    logger.info("Waiting for Envoy Gateway controller to be ready...")
    run_command(
        [
            "kubectl",
            "-n",
            "envoy-gateway-system",
            "rollout",
            "status",
            "deploy/envoy-gateway",
            "--timeout=300s",
        ]
    )
    logger.info("Envoy Gateway installed")


def install_envoy_ai_gateway(envoy_ai_gateway_version: str):
    run_command(
        [
            "helm",
            "upgrade",
            "-i",
            "aieg-crd",
            "oci://docker.io/envoyproxy/ai-gateway-crds-helm",
            "--version",
            envoy_ai_gateway_version,
            "--namespace",
            "envoy-ai-gateway-system",
            "--create-namespace",
            "--wait",
            "--timeout",
            "300s",
        ]
    )
    run_command(
        [
            "helm",
            "upgrade",
            "-i",
            "aieg",
            "oci://docker.io/envoyproxy/ai-gateway-helm",
            "--version",
            envoy_ai_gateway_version,
            "--namespace",
            "envoy-ai-gateway-system",
            "--create-namespace",
            "--wait",
            "--timeout",
            "300s",
        ]
    )
    run_command(
        [
            "kubectl",
            "-n",
            "envoy-ai-gateway-system",
            "rollout",
            "status",
            "deploy/ai-gateway-controller",
            "--timeout=300s",
        ]
    )


def ensure_gateway(kserve_namespace: str, gateway_name: str, gateway_namespace: str):
    logger.info("Creating namespace '%s'...", kserve_namespace)
    ns_yaml = run_command(
        [
            "kubectl",
            "create",
            "namespace",
            kserve_namespace,
            "--dry-run=client",
            "-o",
            "yaml",
        ]
    )
    subprocess.run(["kubectl", "apply", "-f", "-"], input=ns_yaml, text=True, check=True)

    logger.info("Creating Gateway '%s' in namespace '%s'...", gateway_name, gateway_namespace)
    gateway_manifest = f"""apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: envoy
spec:
  controllerName: gateway.envoyproxy.io/gatewayclass-controller
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: {gateway_name}
  namespace: {gateway_namespace}
spec:
  gatewayClassName: envoy
  listeners:
  - name: http
    protocol: HTTP
    port: 80
    allowedRoutes:
      namespaces:
        from: All
  infrastructure:
    labels:
      serving.kserve.io/gateway: {gateway_name}
"""

    subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=gateway_manifest,
        text=True,
        check=True,
    )

    logger.info("Gateway resource created, checking status...")
    run_command(
        [
            "kubectl",
            "-n",
            gateway_namespace,
            "get",
            "gateway",
            gateway_name,
            "-o",
            "wide",
        ]
    )


def assert_gateway_programmed(gateway_name: str, gateway_namespace: str):
    logger.info("Waiting for Gateway '%s' to be programmed...", gateway_name)
    for attempt in RETRY_FOR_TEN_MINUTES:
        with attempt:
            import json

            status_json = run_command(
                [
                    "kubectl",
                    "-n",
                    gateway_namespace,
                    "get",
                    "gateway",
                    gateway_name,
                    "-o",
                    "json",
                ]
            )
            gateway_data = json.loads(status_json)
            conditions = gateway_data.get("status", {}).get("conditions", [])
            logger.info("Gateway conditions: %s", conditions)

            # Check for Accepted condition with True status
            for condition in conditions:
                if condition.get("type") == "Accepted" and condition.get("status") == "True":
                    logger.info("Gateway is Accepted and Programmed")
                    return

            # Log current state for debugging
            if conditions:
                logger.info(
                    "Gateway not ready yet. Conditions: %s",
                    [(c.get("type"), c.get("status"), c.get("reason")) for c in conditions],
                )
            raise AssertionError("Gateway not yet programmed")


def apply_llmisvc_example(manifest_path: str):
    logger.info("Applying LLMInferenceService example from %s...", manifest_path)
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            result = subprocess.run(
                ["kubectl", "apply", "-f", manifest_path],
                check=False,
                text=True,
                capture_output=True,
            )
            if result.returncode == 0:
                break

            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            raise AssertionError(
                "Failed applying LLMInferenceService manifest. "
                f"kubectl exit={result.returncode}; stderr={stderr}; stdout={stdout}"
            )

    logger.info(
        "Waiting for LLMInferenceService routing and workloads readiness (up to 20 minutes)..."
    )
    for attempt in RETRY_FOR_TWENTY_MINUTES:
        with attempt:
            llmisvc_json = run_command(
                [
                    "kubectl",
                    "-n",
                    "default",
                    "get",
                    "llminferenceservice",
                    "test-llm-scheduler",
                    "-o",
                    "json",
                ]
            )
            status = json.loads(llmisvc_json).get("status", {})
            conditions = {
                condition.get("type"): condition.get("status")
                for condition in status.get("conditions", [])
            }

            routes_ready = conditions.get("HTTPRoutesReady") == "True"
            workloads_ready = conditions.get("WorkloadsReady") == "True"

            if routes_ready and workloads_ready:
                logger.info(
                    "LLMInferenceService has HTTP routes and workloads ready; "
                    "proceeding to route checks"
                )
                return

            raise AssertionError(
                "LLMInferenceService not ready enough yet: "
                f"HTTPRoutesReady={conditions.get('HTTPRoutesReady')}, "
                f"WorkloadsReady={conditions.get('WorkloadsReady')}, "
                f"Ready={conditions.get('Ready')}"
            )


def assert_route_programmed():
    output = run_command(
        [
            "kubectl",
            "-n",
            "default",
            "describe",
            "httproute",
            "test-llm-scheduler-kserve-route",
        ]
    )
    assert "Accepted" in output
    assert "ResolvedRefs" in output or "Programmed" in output


def assert_inferencepool_and_workload_resources():
    inferencepools = run_command(["kubectl", "-n", "default", "get", "inferencepool"])
    assert "test-llm-scheduler" in inferencepools

    services = run_command(["kubectl", "-n", "default", "get", "svc"])
    assert "test-llm" in services

    pods = run_command(["kubectl", "-n", "default", "get", "pods", "-o", "wide"])
    assert "test-llm" in pods


def _assert_service_metrics_ports(namespace: str, app_name: str):
    service_json = run_command(["kubectl", "-n", namespace, "get", "svc", app_name, "-o", "json"])
    service = json.loads(service_json)
    service_ports = {port.get("port") for port in service.get("spec", {}).get("ports", [])}

    missing_ports = {
        LLMISVC_CONTROLLER_METRICS_PORT,
        LLMISVC_AGGREGATED_METRICS_PORT,
    } - service_ports
    if missing_ports:
        raise AssertionError(
            "llmisvc service is missing expected metrics ports: "
            f"{sorted(missing_ports)}; found {sorted(service_ports)}"
        )


def _assert_prometheus_exposition(
    url: str,
    endpoint_name: str,
    require_workload_labels: bool,
    accepted_status_codes: set[int],
):
    for attempt in RETRY_FOR_TEN_MINUTES:
        with attempt:
            try:
                request_kwargs = {"timeout": 20}
                if url.startswith("https://"):
                    request_kwargs["verify"] = False

                response = requests.get(url, **request_kwargs)
            except requests.RequestException as exc:
                raise AssertionError(
                    f"{endpoint_name} metrics endpoint is unreachable: {exc}"
                ) from exc

            if response.status_code not in accepted_status_codes:
                raise AssertionError(
                    f"{endpoint_name} metrics endpoint returned HTTP {response.status_code}; "
                    f"expected one of {sorted(accepted_status_codes)}"
                )

            metrics_text = response.text
            if "# HELP" not in metrics_text and "# TYPE" not in metrics_text:
                raise AssertionError(
                    f"{endpoint_name} endpoint did not return Prometheus exposition format"
                )

            if require_workload_labels and (
                "k8s_pod_name=" not in metrics_text and "k8s_namespace=" not in metrics_text
            ):
                raise AssertionError(
                    f"{endpoint_name} endpoint has no aggregated workload labels yet"
                )

            logger.info("%s metrics endpoint is serving expected Prometheus payload", endpoint_name)
            return


def assert_llmisvc_metrics_endpoints(namespace: str, app_name: str = LLMISVC_APP_NAME):
    logger.info(
        "Validating llmisvc service exposes both controller and aggregated metrics ports..."
    )
    _assert_service_metrics_ports(namespace=namespace, app_name=app_name)

    logger.info("Port-forwarding llmisvc service metrics endpoints for direct probe...")
    port_forward = subprocess.Popen(
        [
            "kubectl",
            "-n",
            namespace,
            "port-forward",
            f"svc/{app_name}",
            f"{LOCAL_CONTROLLER_METRICS_PORT}:{LLMISVC_CONTROLLER_METRICS_PORT}",
            f"{LOCAL_AGGREGATED_METRICS_PORT}:{LLMISVC_AGGREGATED_METRICS_PORT}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        _assert_prometheus_exposition(
            url=f"http://127.0.0.1:{LOCAL_CONTROLLER_METRICS_PORT}/metrics",
            endpoint_name="llmisvc-controller",
            require_workload_labels=False,
            accepted_status_codes={200},
        )
        _assert_prometheus_exposition(
            url=f"http://127.0.0.1:{LOCAL_AGGREGATED_METRICS_PORT}/metrics",
            endpoint_name="llmisvc-workload-aggregated",
            require_workload_labels=True,
            accepted_status_codes={200},
        )
    finally:
        with suppress(ProcessLookupError):
            port_forward.terminate()
        with suppress(subprocess.TimeoutExpired, ProcessLookupError):
            port_forward.wait(timeout=20)


def _gateway_ip(gateway_name: str) -> str:
    return run_command(
        [
            "kubectl",
            "-n",
            "envoy-gateway-system",
            "get",
            "svc",
            "-l",
            f"serving.kserve.io/gateway={gateway_name}",
            "-o",
            "jsonpath={.items[0].status.loadBalancer.ingress[0].ip}",
        ],
        check=False,
    )


def _gateway_service_name(gateway_name: str) -> str:
    return run_command(
        [
            "kubectl",
            "-n",
            "envoy-gateway-system",
            "get",
            "svc",
            "-l",
            f"serving.kserve.io/gateway={gateway_name}",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ]
    )


def assert_prediction(gateway_name: str):
    gw_ip = _gateway_ip(gateway_name)
    payload = {
        "model": "facebook/opt-125m",
        "prompt": "Say hello in one short sentence.",
        "max_tokens": 32,
        "temperature": 0.2,
    }

    if gw_ip:
        response = requests.post(
            f"http://{gw_ip}/default/test-llm-scheduler/v1/completions",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        body = response.json()
        assert isinstance(body, dict)
        assert "choices" in body
        assert body["choices"]
        return

    service_name = _gateway_service_name(gateway_name)
    pf = subprocess.Popen(
        [
            "kubectl",
            "-n",
            "envoy-gateway-system",
            "port-forward",
            f"svc/{service_name}",
            "8080:80",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in RETRY_FOR_TEN_MINUTES:
            with _:
                response = requests.post(
                    "http://127.0.0.1:8080/default/test-llm-scheduler/v1/completions",
                    headers={"Content-Type": "application/json"},
                    json=payload,
                    timeout=120,
                )
                response.raise_for_status()
                body = response.json()
                assert isinstance(body, dict)
                assert "choices" in body
                assert body["choices"]
                break
    finally:
        pf.terminate()
        pf.wait(timeout=20)


def dump_debug_state():
    logger.info("=" * 80)
    logger.info("DEBUG STATE DUMP")
    logger.info("=" * 80)
    commands = [
        ["kubectl", "get", "all", "-A"],
        ["kubectl", "get", "events", "-A"],
        ["kubectl", "-n", "kubeflow", "get", "pods", "-o", "wide"],
        ["kubectl", "-n", "kubeflow", "get", "svc", LLMISVC_APP_NAME, "-o", "yaml"],
        ["kubectl", "-n", "kubeflow", "get", "endpoints", LLMISVC_APP_NAME, "-o", "yaml"],
        [
            "kubectl",
            "-n",
            "kubeflow",
            "logs",
            f"deploy/{LLMISVC_APP_NAME}",
            "-c",
            "metrics-proxy",
            "--tail=200",
        ],
        [
            "kubectl",
            "-n",
            "default",
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/part-of=llminferenceservice",
            "-o",
            "wide",
        ],
        ["kubectl", "-n", "default", "get", "llminferenceservice", "-o", "yaml"],
        ["juju", "status", "--relations"],
    ]
    for cmd in commands:
        try:
            logger.info("[DEBUG] %s", " ".join(cmd))
            output = run_command(cmd, check=False)
            logger.info(output)
        except Exception:  # pragma: no cover
            logger.exception("[DEBUG] failed running %s", cmd)


def _kubectl_items(command: list[str]) -> list[str]:
    output = run_command(command, check=False)
    if not output or "No resources found" in output:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def assert_no_charm_resources_left():
    logger.info("Checking that no charm-owned resources remain in the cluster...")
    selector_by_creator = (
        "app.juju.is/created-by in (kserve-controller,kserve-llmisvc,lws-controller)"
    )
    selector_by_instance = (
        "app.kubernetes.io/instance in "
        "(kserve-controller-kubeflow,kserve-llmisvc-kubeflow,lws-controller-kubeflow)"
    )
    namespaced_resource_kinds = (
        "pod,svc,statefulset,configmap,secret,serviceaccount,role,rolebinding"
    )
    cluster_resource_kinds = (
        "clusterrole,clusterrolebinding,validatingwebhookconfiguration,"
        "mutatingwebhookconfiguration,customresourcedefinition"
    )

    checks = [
        (
            "namespaced resources by creator",
            [
                "kubectl",
                "get",
                namespaced_resource_kinds,
                "-A",
                "-l",
                selector_by_creator,
                "--no-headers",
            ],
        ),
        (
            "cluster resources by creator",
            [
                "kubectl",
                "get",
                cluster_resource_kinds,
                "-l",
                selector_by_creator,
                "--no-headers",
            ],
        ),
        (
            "namespaced resources by instance",
            [
                "kubectl",
                "get",
                namespaced_resource_kinds,
                "-A",
                "-l",
                selector_by_instance,
                "--no-headers",
            ],
        ),
        (
            "cluster resources by instance",
            [
                "kubectl",
                "get",
                cluster_resource_kinds,
                "-l",
                selector_by_instance,
                "--no-headers",
            ],
        ),
    ]

    leftovers = []
    for check_name, command in checks:
        found = _kubectl_items(command)
        if found:
            leftovers.append((check_name, found))

    if leftovers:
        details = []
        for check_name, found in leftovers:
            details.append(f"{check_name}: {found}")
        raise AssertionError(
            "Charm resources still present after remove-application: " + " | ".join(details)
        )

    logger.info("No charm-owned resources left in the cluster")
