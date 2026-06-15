#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Assertions used by the bundle integration tests."""

import logging

import requests
from lightkube.operators import in_
from lightkube.resources.admissionregistration_v1 import (
    MutatingWebhookConfiguration,
    ValidatingWebhookConfiguration,
)
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.resources.apps_v1 import StatefulSet
from lightkube.resources.core_v1 import (
    ConfigMap,
    Pod,
    Secret,
    Service,
    ServiceAccount,
)
from lightkube.resources.rbac_authorization_v1 import (
    ClusterRole,
    ClusterRoleBinding,
    Role,
    RoleBinding,
)

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
from .k8s import (
    Gateway,
    HTTPRoute,
    generic_resource_for_crd,
    get_client,
)
from .kubectl import port_forward
from .retry import RETRY_FOR_TEN_MINUTES, RETRY_FOR_THREE_MINUTES

logger = logging.getLogger(__name__)

# Namespaced and cluster-scoped resource kinds that charms own. Used to assert
# the cluster is clean after the applications are removed.
NAMESPACED_RESOURCE_KINDS = (
    Pod,
    Service,
    StatefulSet,
    ConfigMap,
    Secret,
    ServiceAccount,
    Role,
    RoleBinding,
)
CLUSTER_RESOURCE_KINDS = (
    ClusterRole,
    ClusterRoleBinding,
    ValidatingWebhookConfiguration,
    MutatingWebhookConfiguration,
    CustomResourceDefinition,
)

_COMPLETION_PAYLOAD = {
    "prompt": "Say hello in one short sentence.",
    "max_tokens": 32,
    "temperature": 0.2,
}


def assert_gateway_programmed(gateway_name: str, gateway_namespace: str) -> None:
    logger.info("Waiting for Gateway '%s' to be programmed...", gateway_name)
    client = get_client()
    for attempt in RETRY_FOR_TEN_MINUTES:
        with attempt:
            gateway = client.get(Gateway, name=gateway_name, namespace=gateway_namespace)
            conditions = (gateway.status or {}).get("conditions", [])
            logger.info("Gateway conditions: %s", conditions)

            true_conditions = {
                condition.get("type")
                for condition in conditions
                if condition.get("status") == "True"
            }
            # "Accepted" alone only means the spec is valid; the data plane is
            # not necessarily routable until "Programmed" is also True.
            if {"Accepted", "Programmed"} <= true_conditions:
                logger.info("Gateway is Accepted and Programmed")
                return

            logger.info(
                "Gateway not ready yet. Conditions: %s",
                [(c.get("type"), c.get("status"), c.get("reason")) for c in conditions],
            )
            raise AssertionError("Gateway not yet programmed")


def assert_route_programmed(name: str = LLMISVC_NAME) -> None:
    route = get_client().get(HTTPRoute, name=f"{name}-kserve-route", namespace=NAMESPACE_DEFAULT)
    parents = (route.status or {}).get("parents", [])
    condition_types = {
        condition.get("type")
        for parent in parents
        for condition in parent.get("conditions", [])
        if condition.get("status") == "True"
    }
    assert "Accepted" in condition_types
    assert "ResolvedRefs" in condition_types or "Programmed" in condition_types


def assert_inferencepool_and_workload_resources(name: str = LLMISVC_NAME) -> None:
    client = get_client()
    inferencepool_resource = generic_resource_for_crd(
        "inferencepools.inference.networking.x-k8s.io"
    )

    pools = client.list(inferencepool_resource, namespace=NAMESPACE_DEFAULT)
    assert any(name in pool.metadata.name for pool in pools)

    services = client.list(Service, namespace=NAMESPACE_DEFAULT)
    assert any(name in service.metadata.name for service in services)

    pods = client.list(Pod, namespace=NAMESPACE_DEFAULT)
    assert any(name in pod.metadata.name for pod in pods)


def _assert_service_metrics_ports(namespace: str, app_name: str) -> None:
    service = get_client().get(Service, name=app_name, namespace=namespace)
    service_ports = {port.port for port in (service.spec.ports or [])}

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


def _gateway_services(gateway_name: str) -> list:
    return list(
        get_client().list(
            Service,
            namespace=NAMESPACE_ENVOY_GATEWAY,
            labels={"serving.kserve.io/gateway": gateway_name},
        )
    )


def _gateway_ip(gateway_name: str):
    services = _gateway_services(gateway_name)
    if not services:
        return None
    load_balancer = services[0].status.loadBalancer if services[0].status else None
    ingress = (load_balancer.ingress if load_balancer else None) or []
    if not ingress:
        return None
    return ingress[0].ip


def _gateway_service_name(gateway_name: str) -> str:
    services = _gateway_services(gateway_name)
    if not services:
        raise AssertionError(f"No Envoy service found for gateway '{gateway_name}'")
    return services[0].metadata.name


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

    gw_ip = _gateway_ip(gateway_name)
    if gw_ip:
        _post_completion_and_assert(f"http://{gw_ip}{completions_path}", model=model, name=name)
        return

    service_name = _gateway_service_name(gateway_name)
    with port_forward(NAMESPACE_ENVOY_GATEWAY, f"svc/{service_name}", "8080:80"):
        for attempt in RETRY_FOR_TEN_MINUTES:
            with attempt:
                _post_completion_and_assert(
                    f"http://127.0.0.1:8080{completions_path}", model=model, name=name
                )
                break


def _list_resource_names(resource, labels: dict, namespaced: bool) -> list[str]:
    namespace = "*" if namespaced else None
    return [
        f"{resource.__name__}/{obj.metadata.name}"
        for obj in get_client().list(resource, namespace=namespace, labels=labels)
    ]


def assert_no_charm_resources_left() -> None:
    logger.info("Checking that no charm-owned resources remain in the cluster...")
    selector_by_creator = {
        "app.juju.is/created-by": in_(["kserve-controller", "kserve-llmisvc", "lws-controller"])
    }
    selector_by_instance = {
        "app.kubernetes.io/instance": in_(
            [
                "kserve-controller-kubeflow",
                "kserve-llmisvc-kubeflow",
                "lws-controller-kubeflow",
            ]
        )
    }

    checks = [
        ("namespaced resources by creator", NAMESPACED_RESOURCE_KINDS, selector_by_creator, True),
        ("cluster resources by creator", CLUSTER_RESOURCE_KINDS, selector_by_creator, False),
        ("namespaced resources by instance", NAMESPACED_RESOURCE_KINDS, selector_by_instance, True),
        ("cluster resources by instance", CLUSTER_RESOURCE_KINDS, selector_by_instance, False),
    ]

    # Kubernetes garbage-collection of charm-owned resources (especially
    # cluster-scoped ones like clusterroles/webhooks) can lag behind Juju
    # reporting the applications as removed. Retry the check so GC has time to
    # finish before we assert the cluster is clean.
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            leftovers = []
            for check_name, kinds, selector, namespaced in checks:
                found = []
                for resource in kinds:
                    found.extend(_list_resource_names(resource, selector, namespaced))
                if found:
                    leftovers.append((check_name, found))

            if leftovers:
                details = [f"{check_name}: {found}" for check_name, found in leftovers]
                raise AssertionError(
                    "Charm resources still present after remove-application: " + " | ".join(details)
                )

    logger.info("No charm-owned resources left in the cluster")
