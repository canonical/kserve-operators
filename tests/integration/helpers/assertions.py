#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Assertions used by the bundle integration tests."""

import logging
import time
from dataclasses import dataclass

import requests
from lightkube import ApiError
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

from .charms_dependencies import ENVOY_INGRESS
from .constants import (
    LLMISVC_AGGREGATED_METRICS_PORT,
    LLMISVC_APP_NAME,
    LLMISVC_CONTROLLER_METRICS_PORT,
    LLMISVC_MODEL_NAME,
    LLMISVC_NAME,
    LOCAL_AGGREGATED_METRICS_PORT,
    LOCAL_CONTROLLER_METRICS_PORT,
    LOCAL_VLLM_METRICS_PORT,
    NAMESPACE_DEFAULT,
    VLLM_DECODE_PORT,
    VLLM_PREFILL_PORT,
)
from .k8s import (
    Gateway,
    HTTPRoute,
    LLMInferenceService,
    generic_resource_for_crd,
    get_client,
    get_running_workload_pod,
    get_running_workload_pods_by_role,
)
from .kubectl import port_forward, read_node_files
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


def assert_llminferenceservice_absent(name: str, namespace: str) -> None:
    """Block until the named LLMInferenceService no longer exists."""
    client = get_client()
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            try:
                client.get(LLMInferenceService, name=name, namespace=namespace)
            except ApiError as exc:
                if exc.status.code == 404:
                    return
                raise
            raise AssertionError(
                f"LLMInferenceService '{namespace}/{name}' still exists after charm removal"
            )


def assert_secret_absent(name: str, namespace: str) -> None:
    """Block until the named Secret no longer exists."""
    client = get_client()
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            try:
                client.get(Secret, name=name, namespace=namespace)
            except ApiError as exc:
                if exc.status.code == 404:
                    return
                raise
            raise AssertionError(f"Secret '{namespace}/{name}' still exists after charm removal")


def assert_route_programmed(name: str = LLMISVC_NAME, namespace: str = NAMESPACE_DEFAULT) -> None:
    route = get_client().get(HTTPRoute, name=f"{name}-kserve-route", namespace=namespace)
    parents = (route.status or {}).get("parents", [])
    condition_types = {
        condition.get("type")
        for parent in parents
        for condition in parent.get("conditions", [])
        if condition.get("status") == "True"
    }
    assert "Accepted" in condition_types
    assert "ResolvedRefs" in condition_types or "Programmed" in condition_types


def assert_inferencepool_and_workload_resources(
    name: str = LLMISVC_NAME, namespace: str = NAMESPACE_DEFAULT
) -> None:
    client = get_client()
    inferencepool_resource = generic_resource_for_crd(
        "inferencepools.inference.networking.x-k8s.io"
    )

    pools = client.list(inferencepool_resource, namespace=namespace)
    assert any(name in pool.metadata.name for pool in pools)

    services = client.list(Service, namespace=namespace)
    assert any(name in service.metadata.name for service in services)

    pods = client.list(Pod, namespace=namespace)
    assert any(name in pod.metadata.name for pod in pods)


def assert_workload_requests_gpus(name: str, namespace: str, gpus: int = 1) -> None:
    """Assert the running vLLM container requests and is limited to ``gpus`` GPUs."""
    pod_name = get_running_workload_pod(name, namespace)
    pod = get_client().get(Pod, name=pod_name, namespace=namespace)
    main = next(container for container in pod.spec.containers if container.name == "main")
    assert main.resources.requests.get("nvidia.com/gpu") == str(gpus)
    assert main.resources.limits.get("nvidia.com/gpu") == str(gpus)


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


def _prefill_decode_pods(name: str, namespace: str) -> dict:
    pods = get_running_workload_pods_by_role(name, namespace)
    missing = {"prefill", "decode"} - pods.keys()
    if missing:
        raise AssertionError(
            f"LLMInferenceService '{name}' has no Running pod for role(s) {sorted(missing)}"
        )
    return pods


def assert_prefill_decode_on_different_nodes(name: str, namespace: str) -> None:
    """Assert the prefill and decode pods run on different nodes."""
    pods = _prefill_decode_pods(name, namespace)
    nodes = {role: pod.spec.nodeName for role, pod in pods.items()}
    logger.info("Prefill/decode pod placement: %s", nodes)
    assert nodes["prefill"] != nodes["decode"], f"prefill and decode share a node: {nodes}"


def assert_prefill_decode_request_resources(
    name: str, namespace: str, resources: dict[str, str]
) -> None:
    """Assert the vLLM container of both pools requests and is limited to ``resources``."""
    for role, pod in _prefill_decode_pods(name, namespace).items():
        main = next(container for container in pod.spec.containers if container.name == "main")
        for resource, amount in resources.items():
            assert (
                main.resources.requests.get(resource) == amount
            ), f"{role} pod requests {main.resources.requests.get(resource)} {resource}"
            assert (
                main.resources.limits.get(resource) == amount
            ), f"{role} pod is limited to {main.resources.limits.get(resource)} {resource}"


def _sum_metric(metrics_text: str, metric: str) -> float | None:
    """Sum the samples of ``metric`` in a Prometheus payload; None if it is absent."""
    values = [
        float(line.rsplit(" ", 1)[1])
        for line in metrics_text.splitlines()
        if line.startswith((f"{metric}{{", f"{metric} "))
    ]
    return sum(values) if values else None


def _read_pod_metrics(pod_name: str, namespace: str, port: int) -> str:
    """Return the Prometheus payload served on ``port`` of the pod."""
    with port_forward(namespace, f"pod/{pod_name}", f"{LOCAL_VLLM_METRICS_PORT}:{port}"):
        # Give the port-forward a moment to start listening.
        for remaining in reversed(range(30)):
            try:
                response = requests.get(
                    f"http://127.0.0.1:{LOCAL_VLLM_METRICS_PORT}/metrics", timeout=20
                )
                response.raise_for_status()
                return response.text
            except requests.RequestException:
                if not remaining:
                    raise
                time.sleep(2)
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class PrefillDecodeTraffic:
    """Cumulative KV cache traffic counters of a prefill/decode LLMInferenceService."""

    nixl_bytes: float  # bytes vLLM transferred through NIXL, both pools
    nixl_failed: float  # failed NIXL transfers, both pools
    prefill_ib_sent_bytes: int  # data sent by the IB port of the prefill node
    decode_ib_received_bytes: int  # data received by the IB port of the decode node


def _ib_port_data_bytes(node: str, ib_device: str, image: str) -> tuple[int, int]:
    """Return the bytes sent and received by ``ib_device`` (``<hca>:<port>``) on ``node``."""
    hca, _, port = ib_device.partition(":")
    counters = f"/sys/class/infiniband/{hca}/ports/{port or 1}/counters"
    sent, received = read_node_files(
        node, image, f"{counters}/port_xmit_data", f"{counters}/port_rcv_data"
    )
    # The port data counters count 4-byte words.
    return int(sent) * 4, int(received) * 4


def get_prefill_decode_traffic(
    name: str, namespace: str, ib_device: str, image: str
) -> PrefillDecodeTraffic:
    """Read the NIXL metrics of both vLLM servers and the IB counters of their nodes.

    ``image`` runs the pods reading the IB counters; it should already be on the nodes.
    """
    pods = _prefill_decode_pods(name, namespace)
    ports = {"prefill": VLLM_PREFILL_PORT, "decode": VLLM_DECODE_PORT}
    nixl_bytes, nixl_failed = 0.0, 0.0
    for role, pod in pods.items():
        metrics = _read_pod_metrics(pod.metadata.name, namespace, ports[role])
        nixl_bytes += _sum_metric(metrics, "vllm:nixl_bytes_transferred_sum") or 0.0
        nixl_failed += _sum_metric(metrics, "vllm:nixl_num_failed_transfers_total") or 0.0
    prefill_sent, _ = _ib_port_data_bytes(pods["prefill"].spec.nodeName, ib_device, image)
    _, decode_received = _ib_port_data_bytes(pods["decode"].spec.nodeName, ib_device, image)
    traffic = PrefillDecodeTraffic(nixl_bytes, nixl_failed, prefill_sent, decode_received)
    logger.info("Prefill/decode traffic: %s", traffic)
    return traffic


def assert_kv_transferred_over_ib(
    name: str, namespace: str, ib_device: str, image: str, before: PrefillDecodeTraffic
) -> None:
    """Assert the KV cache moved from prefill to decode through NIXL over IB since ``before``.

    NIXL must report newly transferred bytes and no failed transfer. The decode pool
    reads the KV cache from the prefill GPU memory with RDMA, so the IB port of the
    prefill node must have sent, and the one of the decode node received, at least
    as many bytes.
    """
    for attempt in RETRY_FOR_THREE_MINUTES:
        with attempt:
            after = get_prefill_decode_traffic(name, namespace, ib_device, image)
            nixl = after.nixl_bytes - before.nixl_bytes
            sent = after.prefill_ib_sent_bytes - before.prefill_ib_sent_bytes
            received = after.decode_ib_received_bytes - before.decode_ib_received_bytes
            assert after.nixl_failed == 0, f"NIXL reported {after.nixl_failed:.0f} failed transfers"
            assert nixl > 0, "no new KV cache bytes transferred through NIXL yet"
            assert sent >= nixl, (
                f"the prefill node sent {sent} B over {ib_device}, "
                f"less than the {nixl:.0f} B transferred by NIXL"
            )
            assert received >= nixl, (
                f"the decode node received {received} B over {ib_device}, "
                f"less than the {nixl:.0f} B transferred by NIXL"
            )
    logger.info(
        "NIXL transferred %.0f B of KV cache; over %s the prefill node sent %d B "
        "and the decode node received %d B",
        nixl,
        ib_device,
        sent,
        received,
    )


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


def _gateway_ip(gateway_name: str, gateway_namespace: str):
    """Return the Gateway's programmed LoadBalancer address (``.status.addresses``), or None."""
    gateway = get_client().get(Gateway, name=gateway_name, namespace=gateway_namespace)
    addresses = (gateway.status or {}).get("addresses", []) or []
    for address in addresses:
        value = address.get("value")
        if value:
            return value
    return None


def _gateway_lb_service_name(gateway_name: str, gateway_namespace: str) -> str:
    """Return the Envoy-managed LoadBalancer Service backing the gateway."""
    services = list(
        get_client().list(
            Service,
            namespace=gateway_namespace,
            labels={
                "gateway.envoyproxy.io/owning-gateway-name": gateway_name,
                "gateway.envoyproxy.io/owning-gateway-namespace": gateway_namespace,
            },
        )
    )
    if not services:
        raise AssertionError(
            f"No Envoy service found for gateway '{gateway_namespace}/{gateway_name}'"
        )
    return services[0].metadata.name


def _post_completion_and_assert(url: str, model: str, name: str, prompt: str | None = None) -> None:
    payload = {"model": model, **_COMPLETION_PAYLOAD}
    if prompt is not None:
        payload["prompt"] = prompt
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
    gateway_name: str,
    gateway_namespace: str,
    name: str = LLMISVC_NAME,
    model: str = LLMISVC_MODEL_NAME,
    namespace: str = NAMESPACE_DEFAULT,
    prompt: str | None = None,
) -> None:
    completions_path = f"/{namespace}/{name}/v1/completions"

    gw_ip = _gateway_ip(gateway_name, gateway_namespace)
    if gw_ip:
        url = f"http://{gw_ip}{completions_path}"
        for attempt in RETRY_FOR_TEN_MINUTES:
            with attempt:
                _post_completion_and_assert(url, model=model, name=name, prompt=prompt)
                return

    service_name = _gateway_lb_service_name(gateway_name, gateway_namespace)
    with port_forward(gateway_namespace, f"svc/{service_name}", "8080:80"):
        for attempt in RETRY_FOR_TEN_MINUTES:
            with attempt:
                _post_completion_and_assert(
                    f"http://127.0.0.1:8080{completions_path}",
                    model=model,
                    name=name,
                    prompt=prompt,
                )
                return


def assert_llmisvc_serving(
    gateway_namespace: str,
    name: str = LLMISVC_NAME,
    model: str = LLMISVC_MODEL_NAME,
    namespace: str = NAMESPACE_DEFAULT,
) -> None:
    """Assert the LLMInferenceService is routed, has its workload and answers a completion."""
    assert_route_programmed(name=name, namespace=namespace)
    assert_inferencepool_and_workload_resources(name=name, namespace=namespace)
    assert_prediction(
        gateway_name=ENVOY_INGRESS.charm,
        gateway_namespace=gateway_namespace,
        name=name,
        model=model,
        namespace=namespace,
    )


def _list_resource_names(resource, labels: dict, namespaced: bool) -> list[str]:
    namespace = "*" if namespaced else None
    return [
        f"{resource.__name__}/{obj.metadata.name}"
        for obj in get_client().list(resource, namespace=namespace, labels=labels)
    ]


def assert_no_charm_resources_left(model: str) -> None:
    logger.info("Checking that no charm-owned resources remain in the cluster...")
    selector_by_creator = {
        "app.juju.is/created-by": in_(["kserve-controller", "kserve-llmisvc", "lws-controller"])
    }
    # Juju sets app.kubernetes.io/instance to "<app>-<model>", so derive the
    # values from the model name rather than hardcoding a specific model.
    selector_by_instance = {
        "app.kubernetes.io/instance": in_(
            [
                f"kserve-controller-{model}",
                f"kserve-llmisvc-{model}",
                f"lws-controller-{model}",
            ]
        )
    }

    checks = [
        ("namespaced resources by creator", NAMESPACED_RESOURCE_KINDS, selector_by_creator, True),
        ("cluster resources by creator", CLUSTER_RESOURCE_KINDS, selector_by_creator, False),
        ("namespaced resources by instance", NAMESPACED_RESOURCE_KINDS, selector_by_instance, True),
        ("cluster resources by instance", CLUSTER_RESOURCE_KINDS, selector_by_instance, False),
    ]

    # Cluster GC of charm-owned resources (especially cluster-scoped ones) can
    # lag behind Juju reporting the apps removed, so retry before asserting.
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
