# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Render context for the KEDA ScaledObjects that autoscale the vLLM workers.

Each worker Deployment KServe creates (``<app>-kserve`` and, in prefill/decode mode,
``<app>-kserve-prefill``) gets its own ScaledObject, named after the Deployment, whose
Prometheus query only covers that Deployment's pods.
"""

from string import Template
from typing import List

from config import KV_CACHE_USAGE, NUM_REQUESTS, NUM_REQUESTS_WAITING, CharmConfig

# Labels the kserve-llmisvc metrics aggregator attaches to every vLLM sample.
_POD_SELECTOR = 'k8s_namespace="$namespace",k8s_pod_name=~"$pods"'

# Queries must return a single series: KEDA rejects results with more than one.
METRIC_QUERIES = {
    NUM_REQUESTS: (
        'sum({__name__=~"vllm:num_requests_running|vllm:num_requests_waiting",'
        f"{_POD_SELECTOR}}})"
    ),
    NUM_REQUESTS_WAITING: f"sum(vllm:num_requests_waiting{{{_POD_SELECTOR}}})",
    KV_CACHE_USAGE: f"sum(vllm:kv_cache_usage_perc{{{_POD_SELECTOR}}})",
}


def worker_deployments(app_name: str, prefill_decode: bool) -> List[str]:
    """Names of the worker Deployments KServe creates for the LLMInferenceService."""
    decode = f"{app_name}-kserve"
    return [decode, f"{decode}-prefill"] if prefill_decode else [decode]


def scaled_objects_context(
    app_name: str, namespace: str, config: CharmConfig, server_address: str
) -> dict:
    """Render context for the ScaledObjects template."""
    query = Template(METRIC_QUERIES[config.autoscaling_metric])
    return {
        "namespace": namespace,
        "scaled_objects": [
            {
                "name": deployment,
                # Pod names are <deployment>-<replicaset hash>-<suffix>.
                "query": query.substitute(namespace=namespace, pods=f"{deployment}-[^-]+-[^-]+"),
            }
            for deployment in worker_deployments(app_name, config.enable_prefill_decode)
        ],
        "min_replicas": config.min_replicas,
        "max_replicas": config.max_replicas,
        "polling_interval": config.autoscaling_polling_interval,
        "scale_down_delay": config.autoscaling_scale_down_delay,
        "server_address": server_address,
        "threshold": f"{config.autoscaling_threshold:g}",
    }
