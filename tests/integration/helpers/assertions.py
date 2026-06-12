#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Assertions used by the bundle integration tests."""

import logging

import requests

from .constants import (
    LLMISVC_AGGREGATED_METRICS_PORT,
    LLMISVC_APP_NAME,
    LLMISVC_CONTROLLER_METRICS_PORT,
    LLMISVC_MODEL_NAME,
    LLMISVC_NAME,
    LOCAL_AGGREGATED_METRICS_PORT,
    LOCAL_CONTROLLER_METRICS_PORT,
    NAMESPACE_DEFAULT,
    NAMESPACE_ENVOY_GATEWAY,
)
from .kubectl import kubectl, kubectl_get_json, port_forward
from .retry import RETRY_FOR_TEN_MINUTES, RETRY_FOR_THREE_MINUTES

logger = logging.getLogger(__name__)

_COMPLETION_PAYLOAD = {
    "prompt": "Say hello in one short sentence.",
    "max_tokens": 32,
    "temperature": 0.2,
}


def assert_gateway_programmed(gateway_name: str, gateway_namespace: str) -> None:
    logger.info("Waiting for Gateway '%s' to be programmed...", gateway_name)
    for attempt in RETRY_FOR_TEN_MINUTES:
        with attempt:
            gateway_data = kubectl_get_json("-n", gateway_namespace, "get", "gateway", gateway_name)
            conditions = gateway_data.get("status", {}).get("conditions", [])
            logger.info("Gateway conditions: %s", conditions)

            for condition in conditions:
                if condition.get("type") == "Accepted" and condition.get("status") == "True":
                    logger.info("Gateway is Accepted and Programmed")
                    return

            if conditions:
                logger.info(
                    "Gateway not ready yet. Conditions: %s",
                    [(c.get("type"), c.get("status"), c.get("reason")) for c in conditions],
                )
            raise AssertionError("Gateway not yet programmed")


def assert_route_programmed(name: str = LLMISVC_NAME) -> None:
    output = kubectl(["-n", NAMESPACE_DEFAULT, "describe", "httproute", f"{name}-kserve-route"])
    assert "Accepted" in output
    assert "ResolvedRefs" in output or "Programmed" in output


def assert_inferencepool_and_workload_resources(name: str = LLMISVC_NAME) -> None:
    inferencepools = kubectl(["-n", NAMESPACE_DEFAULT, "get", "inferencepool"])
    assert name in inferencepools

    services = kubectl(["-n", NAMESPACE_DEFAULT, "get", "svc"])
    assert name in services

    pods = kubectl(["-n", NAMESPACE_DEFAULT, "get", "pods", "-o", "wide"])
    assert name in pods


def _assert_service_metrics_ports(namespace: str, app_name: str) -> None:
    service = kubectl_get_json("-n", namespace, "get", "svc", app_name)
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
) -> None:
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


def assert_llmisvc_metrics_endpoints(namespace: str, app_name: str = LLMISVC_APP_NAME) -> None:
    logger.info(
        "Validating llmisvc service exposes both controller and aggregated metrics ports..."
    )
    _assert_service_metrics_ports(namespace=namespace, app_name=app_name)

    logger.info("Port-forwarding llmisvc service metrics endpoints for direct probe...")
    with port_forward(
        namespace,
        f"svc/{app_name}",
        f"{LOCAL_CONTROLLER_METRICS_PORT}:{LLMISVC_CONTROLLER_METRICS_PORT}",
        f"{LOCAL_AGGREGATED_METRICS_PORT}:{LLMISVC_AGGREGATED_METRICS_PORT}",
    ):
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


def _gateway_field(gateway_name: str, jsonpath: str, check: bool = True) -> str:
    return kubectl(
        [
            "-n",
            NAMESPACE_ENVOY_GATEWAY,
            "get",
            "svc",
            "-l",
            f"serving.kserve.io/gateway={gateway_name}",
            "-o",
            f"jsonpath={jsonpath}",
        ],
        check=check,
    )


def _post_completion_and_assert(url: str, model: str, name: str) -> None:
    payload = {"model": model, **_COMPLETION_PAYLOAD}
    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    response.raise_for_status()
    body = response.json()
    assert isinstance(body, dict)
    assert "choices" in body
    assert body["choices"]


def assert_prediction(
    gateway_name: str, name: str = LLMISVC_NAME, model: str = LLMISVC_MODEL_NAME
) -> None:
    completions_path = f"/default/{name}/v1/completions"

    gw_ip = _gateway_field(
        gateway_name, "{.items[0].status.loadBalancer.ingress[0].ip}", check=False
    )
    if gw_ip:
        _post_completion_and_assert(f"http://{gw_ip}{completions_path}", model=model, name=name)
        return

    service_name = _gateway_field(gateway_name, "{.items[0].metadata.name}")
    with port_forward(NAMESPACE_ENVOY_GATEWAY, f"svc/{service_name}", "8080:80"):
        for attempt in RETRY_FOR_TEN_MINUTES:
            with attempt:
                _post_completion_and_assert(
                    f"http://127.0.0.1:8080{completions_path}", model=model, name=name
                )
                break


def _kubectl_items(args: list[str]) -> list[str]:
    output = kubectl(args, check=False)
    if not output or "No resources found" in output:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def assert_no_charm_resources_left() -> None:
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
            ["get", namespaced_resource_kinds, "-A", "-l", selector_by_creator, "--no-headers"],
        ),
        (
            "cluster resources by creator",
            ["get", cluster_resource_kinds, "-l", selector_by_creator, "--no-headers"],
        ),
        (
            "namespaced resources by instance",
            ["get", namespaced_resource_kinds, "-A", "-l", selector_by_instance, "--no-headers"],
        ),
        (
            "cluster resources by instance",
            ["get", cluster_resource_kinds, "-l", selector_by_instance, "--no-headers"],
        ),
    ]

    # Kubernetes garbage-collection of charm-owned resources (especially
    # cluster-scoped ones like clusterroles/webhooks) can lag behind Juju
    # reporting the applications as removed. Retry the check so GC has time to
    # finish before we assert the cluster is clean.
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            leftovers = []
            for check_name, args in checks:
                found = _kubectl_items(args)
                if found:
                    leftovers.append((check_name, found))

            if leftovers:
                details = [f"{check_name}: {found}" for check_name, found in leftovers]
                raise AssertionError(
                    "Charm resources still present after remove-application: " + " | ".join(details)
                )

    logger.info("No charm-owned resources left in the cluster")
