# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for forwarding vLLM workload logs to Loki."""

import json
from pathlib import Path
from unittest.mock import patch

from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from jinja2 import Template
from ops.model import ActiveStatus
from ops.testing import Relation, State

LOKI_URL = "http://loki-0.loki-endpoints:3100/loki/api/v1/push"
TEMPLATE_PATH = Path(__file__).parents[2] / "src/templates/llmisvc_configs_manifests.yaml.j2"


def _logging_relation(endpoint: str | None = None) -> Relation:
    unit_data = {"endpoint": endpoint} if endpoint else {}
    return Relation(
        endpoint="logging",
        interface="loki_push_api",
        remote_app_name="loki",
        remote_units_data={0: unit_data},
    )


def _render_template(loki_url: str = "") -> str:
    return Template(TEMPLATE_PATH.read_text()).render(
        namespace="test-model",
        scheduler_image="scheduler-image",
        workload_image="workload-image",
        llm_routing_sidecar="routing-sidecar-image",
        loki_url=loki_url,
    )


def test_vllm_templates_render_loki_url():
    """Every vLLM workload template should receive the COS Loki endpoint."""
    rendered = _render_template(LOKI_URL)

    assert rendered.count("name: LOKI_URL") == 9
    assert rendered.count(f"value: {LOKI_URL}") == 9


def test_vllm_templates_omit_loki_url_without_cos():
    """vLLM workloads should not configure log forwarding without a Loki endpoint."""
    assert "LOKI_URL" not in _render_template()


def test_logging_relation_passes_loki_url_to_scheduler_context(
    ctx, both_containers, controller_relation_ready, lws_relation_ready
):
    """A ready logging relation should refresh the scheduler render context."""
    logging_relation = _logging_relation(json.dumps({"url": LOKI_URL}))
    state_in = State(
        leader=True,
        containers=both_containers,
        relations=[controller_relation_ready, lws_relation_ready, logging_relation],
    )
    original_init = KubernetesResourceHandler.__init__

    with patch.object(KubernetesResourceHandler, "__init__", autospec=True) as init:
        init.side_effect = original_init
        out = ctx.run(ctx.on.relation_changed(logging_relation), state_in)

    assert isinstance(out.unit_status, ActiveStatus)
    scheduler_contexts = [
        call.kwargs["context"]
        for call in init.call_args_list
        if call.kwargs.get("template_files") == ["src/templates/llmisvc_configs_manifests.yaml.j2"]
    ]
    assert scheduler_contexts == [
        {
            "namespace": out.model.name,
            "scheduler_image": scheduler_contexts[0]["scheduler_image"],
            "workload_image": scheduler_contexts[0]["workload_image"],
            "llm_routing_sidecar": scheduler_contexts[0]["llm_routing_sidecar"],
            "loki_url": LOKI_URL,
        }
    ]


def test_malformed_logging_endpoint_omits_loki_url(
    ctx, both_containers, controller_relation_ready, lws_relation_ready
):
    """Malformed logging relation data should not configure a vLLM forwarder."""
    logging_relation = _logging_relation("not-json")
    state_in = State(
        leader=True,
        containers=both_containers,
        relations=[controller_relation_ready, lws_relation_ready, logging_relation],
    )
    original_init = KubernetesResourceHandler.__init__

    with patch.object(KubernetesResourceHandler, "__init__", autospec=True) as init:
        init.side_effect = original_init
        ctx.run(ctx.on.relation_changed(logging_relation), state_in)

    scheduler_context = next(
        call.kwargs["context"]
        for call in init.call_args_list
        if call.kwargs.get("template_files") == ["src/templates/llmisvc_configs_manifests.yaml.j2"]
    )
    assert scheduler_context["loki_url"] == ""
