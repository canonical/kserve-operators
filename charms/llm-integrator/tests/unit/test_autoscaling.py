# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for KEDA autoscaling of the vLLM workers."""

import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from jinja2 import Template
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.testing import Relation, State

from charm import KEDA_RELATION, PROMETHEUS_API_RELATION, ScaledObject

from .helpers import assert_status

TEMPLATE = Path(__file__).resolve().parents[2] / "src/templates/scaled_objects.yaml.j2"
PROMETHEUS_URL = "http://prometheus-0.prometheus-endpoints.cos.svc.cluster.local:9090"
DECODE = "llm-integrator-kserve"
PREFILL = "llm-integrator-kserve-prefill"
AUTOSCALING = {"enable-autoscaling": True}


def _keda_relation(ready: bool = True) -> Relation:
    return Relation(
        endpoint=KEDA_RELATION,
        interface="keda-sync",
        remote_app_name="keda-controller",
        remote_app_data={"ready": str(ready).lower(), "namespace": "kubeflow"},
    )


def _prometheus_relation(url: str = PROMETHEUS_URL) -> Relation:
    return Relation(
        endpoint=PROMETHEUS_API_RELATION,
        interface="prometheus_api",
        remote_app_name="prometheus",
        remote_app_data={"direct_url": url} if url else {},
    )


@pytest.fixture
def autoscaling_relations():
    return [_keda_relation(), _prometheus_relation()]


@pytest.fixture
def serving(mock_krh_lightkube_client):
    """The LLMInferenceService is Ready; ScaledObjects report no conditions yet."""
    scaled_object_conditions = []

    def get(resource, name, namespace):
        if resource is ScaledObject:
            return SimpleNamespace(status={"conditions": scaled_object_conditions})
        return SimpleNamespace(status={"conditions": [{"type": "Ready", "status": "True"}]})

    mock_krh_lightkube_client.get.side_effect = get
    return scaled_object_conditions


def _state(valid_config, relations, config=None) -> State:
    return State(leader=True, config={**valid_config, **(config or {})}, relations=relations)


def _deleted_scaled_objects(client) -> list:
    return [
        call.kwargs["name"]
        for call in client.delete.call_args_list
        if call.args[0] is ScaledObject
    ]


def _render(ctx, state: State) -> tuple:
    """Run the charm; return its output state and the ScaledObjects it applied."""
    with ctx(ctx.on.config_changed(), state) as manager:
        out = manager.run()
        context = manager.charm.scaled_objects_handler.context
    rendered = Template(TEMPLATE.read_text()).render(context)
    return out, [doc for doc in yaml.safe_load_all(rendered) if doc]


def _trigger(scaled_object: dict) -> dict:
    [trigger] = scaled_object["spec"]["triggers"]
    assert trigger["type"] == "prometheus"
    return trigger["metadata"]


def test_disabled_autoscaling_removes_scaled_objects(
    ctx, valid_config, llmisvc_relation_ready, serving, mock_krh_apply, mock_krh_lightkube_client
):
    """Without autoscaling only the LLMInferenceService is applied; stale ScaledObjects go."""
    out = ctx.run(ctx.on.config_changed(), _state(valid_config, [llmisvc_relation_ready]))

    mock_krh_apply.assert_called_once()
    assert _deleted_scaled_objects(mock_krh_lightkube_client) == [DECODE, PREFILL]
    assert out.unit_status == ActiveStatus()


def test_cleanup_without_keda_installed(
    ctx, valid_config, llmisvc_relation_ready, serving, mock_krh_lightkube_client
):
    """Without the KEDA CRD the API server answers a plain-text 404 that is not an ApiError."""
    request = httpx.Request("DELETE", "https://k8s/apis/keda.sh/v1alpha1/scaledobjects/x")
    not_served = httpx.HTTPStatusError(
        "404 Not Found", request=request, response=httpx.Response(404, text="404 page not found")
    )

    def delete(resource, **_):
        if resource is ScaledObject:
            raise not_served

    mock_krh_lightkube_client.delete.side_effect = delete

    out = ctx.run(ctx.on.config_changed(), _state(valid_config, [llmisvc_relation_ready]))
    assert out.unit_status == ActiveStatus()


@pytest.mark.parametrize(
    "relations, expected_status, expected_msg, deleted",
    [
        (
            [],
            BlockedStatus,
            "Please relate to keda-controller:keda to enable autoscaling",
            [DECODE, PREFILL],
        ),
        (
            [_keda_relation(ready=False)],
            WaitingStatus,
            "Waiting for keda-controller to report ready=true",
            [PREFILL],
        ),
        (
            [_keda_relation()],
            BlockedStatus,
            "Please relate to Prometheus over prometheus-api",
            [DECODE, PREFILL],
        ),
        (
            [_keda_relation(), _prometheus_relation(url="")],
            WaitingStatus,
            "Waiting for prometheus-api relation data",
            [PREFILL],
        ),
        (
            [_keda_relation(), _prometheus_relation(url="not-a-url")],
            WaitingStatus,
            "Waiting for prometheus-api relation data",
            [PREFILL],
        ),
    ],
)
def test_missing_autoscaling_dependencies_do_not_stop_serving(
    ctx,
    valid_config,
    llmisvc_relation_ready,
    serving,
    mock_krh_apply,
    mock_krh_lightkube_client,
    relations,
    expected_status,
    expected_msg,
    deleted,
):
    """The workload keeps serving; a missing relation removes the ScaledObjects, while a
    dependency that is only waiting keeps the current worker's ScaledObject."""
    state = _state(valid_config, [llmisvc_relation_ready, *relations], AUTOSCALING)
    out = ctx.run(ctx.on.config_changed(), state)

    mock_krh_apply.assert_called_once()
    assert _deleted_scaled_objects(mock_krh_lightkube_client) == deleted
    assert_status(out, expected_status, expected_msg)


def test_workload_status_takes_precedence(ctx, valid_config, llmisvc_relation_ready):
    """While the workload is not serving its status wins over autoscaling problems."""
    out = ctx.run(
        ctx.on.config_changed(), _state(valid_config, [llmisvc_relation_ready], AUTOSCALING)
    )
    assert_status(out, WaitingStatus, "to be created")


def test_keda_relation_broken_removes_scaled_objects(
    ctx, valid_config, llmisvc_relation_ready, serving, mock_krh_lightkube_client
):
    keda = _keda_relation()
    state = _state(
        valid_config, [llmisvc_relation_ready, keda, _prometheus_relation()], AUTOSCALING
    )
    out = ctx.run(ctx.on.relation_broken(keda), state)

    assert _deleted_scaled_objects(mock_krh_lightkube_client) == [DECODE, PREFILL]
    assert_status(out, BlockedStatus, "keda-controller:keda")


def test_scaled_object_for_the_worker(
    ctx,
    valid_config,
    llmisvc_relation_ready,
    autoscaling_relations,
    serving,
    mock_krh_apply,
    mock_krh_lightkube_client,
):
    """One ScaledObject scales the worker Deployment on the default metric."""
    state = _state(valid_config, [llmisvc_relation_ready, *autoscaling_relations], AUTOSCALING)
    out, [scaled_object] = _render(ctx, state)

    assert mock_krh_apply.call_count == 2
    assert _deleted_scaled_objects(mock_krh_lightkube_client) == [PREFILL]
    assert out.unit_status == ActiveStatus("Autoscaling 1-3 replicas")
    assert scaled_object["metadata"]["name"] == DECODE
    assert scaled_object["spec"] == {
        "scaleTargetRef": {"name": DECODE},
        "minReplicaCount": 1,
        "maxReplicaCount": 3,
        "pollingInterval": 15,
        "advanced": {
            "restoreToOriginalReplicaCount": True,
            "horizontalPodAutoscalerConfig": {
                "behavior": {"scaleDown": {"stabilizationWindowSeconds": 300}}
            },
        },
        "triggers": [
            {
                "type": "prometheus",
                "metadata": {
                    "serverAddress": PROMETHEUS_URL,
                    "query": (
                        'sum({__name__=~"vllm:num_requests_running|vllm:num_requests_waiting",'
                        f'k8s_namespace="{state.model.name}",'
                        f'k8s_pod_name=~"{DECODE}-[^-]+-[^-]+"}})'
                    ),
                    "threshold": "2",
                    "ignoreNullValues": "false",
                },
            }
        ],
    }


def test_prefill_and_decode_scale_independently(
    ctx, valid_config, llmisvc_relation_ready, autoscaling_relations, serving
):
    """Each worker gets its own ScaledObject whose query only matches its own pods."""
    config = {**AUTOSCALING, "enable-prefill-decode": True}
    state = _state(valid_config, [llmisvc_relation_ready, *autoscaling_relations], config)
    _, [decode, prefill] = _render(ctx, state)

    assert decode["spec"]["scaleTargetRef"] == {"name": DECODE}
    assert prefill["spec"]["scaleTargetRef"] == {"name": PREFILL}
    decode_pods = re.search(r'k8s_pod_name=~"([^"]+)"', _trigger(decode)["query"]).group(1)
    prefill_pods = re.search(r'k8s_pod_name=~"([^"]+)"', _trigger(prefill)["query"]).group(1)
    # PromQL regex matchers are fully anchored, like re.fullmatch.
    assert re.fullmatch(decode_pods, f"{DECODE}-7d9f8b6c4-x2k9p")
    assert not re.fullmatch(decode_pods, f"{PREFILL}-5b8c7d9f6-q4w7z")
    assert re.fullmatch(prefill_pods, f"{PREFILL}-5b8c7d9f6-q4w7z")


@pytest.mark.parametrize(
    "config, metric, threshold",
    [
        ({}, "vllm:num_requests_running|vllm:num_requests_waiting", "2"),
        (
            {"accelerator": "nvidia-gpu"},
            "vllm:num_requests_running|vllm:num_requests_waiting",
            "16",
        ),
        ({"autoscaling-metric": "num-requests-waiting"}, "vllm:num_requests_waiting{", "2"),
        ({"autoscaling-metric": "kv-cache-usage"}, "vllm:kv_cache_usage_perc{", "0.8"),
        ({"autoscaling-target": 4.5}, "vllm:num_requests_running", "4.5"),
    ],
)
def test_metric_presets_and_targets(
    ctx,
    valid_config,
    llmisvc_relation_ready,
    autoscaling_relations,
    serving,
    gpu_cluster,
    config,
    metric,
    threshold,
):
    state = _state(
        valid_config, [llmisvc_relation_ready, *autoscaling_relations], {**AUTOSCALING, **config}
    )
    _, [scaled_object] = _render(ctx, state)
    assert metric in _trigger(scaled_object)["query"]
    assert _trigger(scaled_object)["threshold"] == threshold


def test_custom_query_fills_in_placeholders(
    ctx, valid_config, llmisvc_relation_ready, autoscaling_relations, serving
):
    config = {
        **AUTOSCALING,
        "autoscaling-query": 'max(my_metric{ns="$namespace",pod=~"$pods",cost="$$5"})',
        "autoscaling-target": 10.0,
    }
    state = _state(valid_config, [llmisvc_relation_ready, *autoscaling_relations], config)
    _, [scaled_object] = _render(ctx, state)
    assert _trigger(scaled_object) == {
        "serverAddress": PROMETHEUS_URL,
        "query": (
            f'max(my_metric{{ns="{state.model.name}",pod=~"{DECODE}-[^-]+-[^-]+",cost="$5"}})'
        ),
        "threshold": "10",
        "ignoreNullValues": "false",
    }


def test_failing_scaled_object_waits(
    ctx, valid_config, llmisvc_relation_ready, autoscaling_relations, serving
):
    """A ScaledObject KEDA marks not ready, e.g. on an empty query result, is reported."""
    serving.append(
        {
            "type": "Ready",
            "status": "False",
            "reason": "TriggerError",
            "message": "Triggers defined in ScaledObject are not working correctly",
        }
    )
    state = _state(valid_config, [llmisvc_relation_ready, *autoscaling_relations], AUTOSCALING)
    out = ctx.run(ctx.on.config_changed(), state)
    assert_status(
        out,
        WaitingStatus,
        f"Autoscaling of {DECODE} is not working: Triggers defined in ScaledObject",
    )


@pytest.mark.parametrize(
    "config, expected_msg",
    [
        ({"min-replicas": 0}, "min-replicas must be >= 1"),
        ({"min-replicas": 3, "max-replicas": 2}, "max-replicas (2) must be >= min-replicas (3)"),
        (
            {"autoscaling-metric": "gpu-usage"},
            "autoscaling-metric must be one of: num-requests, num-requests-waiting, "
            "kv-cache-usage",
        ),
        ({"autoscaling-target": -1.0}, "autoscaling-target must be > 0"),
        ({"autoscaling-polling-interval": 0}, "autoscaling-polling-interval must be >= 1"),
        (
            {"autoscaling-scale-down-delay": 3601},
            "autoscaling-scale-down-delay must be between 0 and 3600",
        ),
        (
            {"autoscaling-query": "sum(up)"},
            "autoscaling-target must be set when autoscaling-query is used",
        ),
        (
            {"autoscaling-query": 'sum(up{pod=~"$pod"})', "autoscaling-target": 1.0},
            "autoscaling-query may only use the $namespace and $pods placeholders",
        ),
    ],
)
def test_invalid_autoscaling_config_blocks(
    ctx, valid_config, llmisvc_relation_ready, mock_krh_apply, config, expected_msg
):
    state = _state(valid_config, [llmisvc_relation_ready], {**AUTOSCALING, **config})
    out = ctx.run(ctx.on.config_changed(), state)
    assert_status(out, BlockedStatus, expected_msg)
    mock_krh_apply.assert_not_called()


def test_autoscaling_options_not_validated_when_disabled(
    ctx, valid_config, llmisvc_relation_ready, mock_krh_apply
):
    config = {"min-replicas": 0, "max-replicas": 0, "autoscaling-metric": "gpu-usage"}
    ctx.run(ctx.on.config_changed(), _state(valid_config, [llmisvc_relation_ready], config))
    mock_krh_apply.assert_called_once()


def test_capacity_check_counts_min_replicas(ctx, valid_config, llmisvc_relation_ready):
    """The cluster must fit min-replicas workers; the default node fits 8 CPU workers."""
    config = {**AUTOSCALING, "min-replicas": 9, "max-replicas": 9}
    out = ctx.run(ctx.on.config_changed(), _state(valid_config, [llmisvc_relation_ready], config))
    assert_status(out, BlockedStatus, "Cluster fits 8 of 9 workers")
