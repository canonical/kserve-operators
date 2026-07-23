# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Pebble layer, ports and metrics-provider assertions."""

import dataclasses

from ops.model import ActiveStatus, MaintenanceStatus
from ops.testing import Relation, State

from charm import (
    METRICS_PORT,
    METRICS_PROXY_CONTAINER,
    METRICS_PROXY_PORT,
    METRICS_PROXY_SCRAPE_TIMEOUT,
    WORKLOAD_POD_LABEL_SELECTOR,
)

from .helpers import assert_status, get_layer

# ---------------------------------------------------------------------------
# llmisvc-controller layer
# ---------------------------------------------------------------------------


def test_controller_layer_has_expected_service(ctx, ready_state):
    """The controller container should run /manager with metrics + leader-elect."""
    out = ctx.run(ctx.on.install(), ready_state)
    plan = get_layer(out, "llmisvc-controller")

    assert "llmisvc-controller" in plan.services
    svc = plan.services["llmisvc-controller"]
    assert (
        svc.command
        == f"/manager --metrics-addr=:{METRICS_PORT} --metrics-secure=false --leader-elect"
    )
    assert svc.startup == "enabled"
    assert svc.environment["POD_NAMESPACE"]  # rendered from model.name


def test_controller_layer_defines_health_checks(ctx, ready_state):
    """Readiness + liveness HTTP checks must be present on the controller layer."""
    out = ctx.run(ctx.on.install(), ready_state)
    plan = get_layer(out, "llmisvc-controller")

    assert "llmisvc-controller-ready" in plan.checks
    assert "llmisvc-controller-alive" in plan.checks
    assert plan.checks["llmisvc-controller-ready"].http["url"] == "http://localhost:8081/readyz"
    assert plan.checks["llmisvc-controller-alive"].http["url"] == "http://localhost:8081/healthz"


# ---------------------------------------------------------------------------
# metrics-proxy layer
# ---------------------------------------------------------------------------


def test_metrics_proxy_layer_environment(ctx, ready_state):
    """metrics-proxy sidecar should be configured to discover workload pods."""
    out = ctx.run(ctx.on.install(), ready_state)
    plan = get_layer(out, METRICS_PROXY_CONTAINER)

    svc = plan.services[METRICS_PROXY_CONTAINER]
    assert svc.command == "metrics-proxy"
    assert svc.startup == "enabled"
    assert svc.environment == {
        "POD_LABEL_SELECTOR": WORKLOAD_POD_LABEL_SELECTOR,
        "SCRAPE_TIMEOUT": METRICS_PROXY_SCRAPE_TIMEOUT,
        "PORT": str(METRICS_PROXY_PORT),
    }


def test_metrics_proxy_layer_skipped_when_container_unreachable(
    ctx,
    base_state,
    controller_relation_ready,
    lws_relation_ready,
    metrics_proxy_container_disconnected,
):
    """If metrics-proxy can't be reached yet, no layer should be added but reconcile continues."""
    containers = [c for c in base_state.containers if c.name != METRICS_PROXY_CONTAINER]
    containers.append(metrics_proxy_container_disconnected)
    state_in = dataclasses.replace(
        base_state,
        containers=containers,
        relations=[controller_relation_ready, lws_relation_ready],
    )

    out = ctx.run(ctx.on.install(), state_in)
    assert_status(out, ActiveStatus)

    proxy_plan = get_layer(out, METRICS_PROXY_CONTAINER)
    assert METRICS_PROXY_CONTAINER not in proxy_plan.services


# ---------------------------------------------------------------------------
# Controller container disconnected
# ---------------------------------------------------------------------------


def test_controller_container_unreachable_blocks_layer(
    ctx,
    base_state,
    controller_relation_ready,
    lws_relation_ready,
    controller_container_disconnected,
):
    """If the controller container is unreachable, cert push short-circuits and no layer."""
    containers = [c for c in base_state.containers if c.name != "llmisvc-controller"]
    containers.append(controller_container_disconnected)
    state_in = dataclasses.replace(
        base_state,
        containers=containers,
        relations=[controller_relation_ready, lws_relation_ready],
    )

    out = ctx.run(ctx.on.install(), state_in)
    # Depending on mocked handler behavior, reconcile can remain in maintenance
    # or complete and become active; either way the controller layer must not be added.
    assert isinstance(out.unit_status, (ActiveStatus, MaintenanceStatus))
    plan = get_layer(out, "llmisvc-controller")
    assert "llmisvc-controller" not in plan.services


# ---------------------------------------------------------------------------
# MetricsEndpointProvider scrape jobs
# ---------------------------------------------------------------------------


def test_metrics_endpoint_relation_data_has_both_jobs(
    ctx, both_containers, controller_relation_ready, lws_relation_ready
):
    """The metrics-endpoint relation should advertise both scrape jobs."""
    metrics_rel = Relation(endpoint="metrics-endpoint", interface="prometheus_scrape")
    state_in = State(
        leader=True,
        containers=both_containers,
        relations=[controller_relation_ready, lws_relation_ready, metrics_rel],
    )

    out = ctx.run(ctx.on.relation_joined(metrics_rel), state_in)

    out_rel = out.get_relation(metrics_rel.id)
    scrape_jobs_raw = out_rel.local_app_data.get("scrape_jobs", "")
    assert f"*:{METRICS_PORT}" in scrape_jobs_raw
    assert f"*:{METRICS_PROXY_PORT}" in scrape_jobs_raw
    assert "llmisvc-controller" in scrape_jobs_raw
    assert "llmisvc-workload-aggregated" in scrape_jobs_raw
