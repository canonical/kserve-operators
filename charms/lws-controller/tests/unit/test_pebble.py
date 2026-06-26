# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Pebble layer and metrics-provider assertions."""

import dataclasses

from ops.testing import Relation, State

from charm import HEALTH_PORT, MANAGER_CONFIG_DEST, METRICS_PORT

from .helpers import get_layer


def test_controller_layer_has_expected_service(ctx, base_state):
    """The controller container should run /manager with our config and zap log level."""
    out = ctx.run(ctx.on.install(), base_state)
    plan = get_layer(out, "lws-controller")

    assert "lws-controller" in plan.services
    svc = plan.services["lws-controller"]
    assert MANAGER_CONFIG_DEST in svc.command
    assert "/manager" in svc.command
    assert "--zap-log-level=2" in svc.command
    assert svc.startup == "enabled"
    assert svc.environment["POD_NAMESPACE"]


def test_controller_layer_defines_health_checks(ctx, base_state):
    """Readiness + liveness HTTP checks must be present on the controller layer."""
    out = ctx.run(ctx.on.install(), base_state)
    plan = get_layer(out, "lws-controller")

    assert "lws-controller-ready" in plan.checks
    assert "lws-controller-alive" in plan.checks
    expected_ready = f"http://localhost:{HEALTH_PORT}/readyz"
    expected_alive = f"http://localhost:{HEALTH_PORT}/healthz"
    assert plan.checks["lws-controller-ready"].http["url"] == expected_ready
    assert plan.checks["lws-controller-alive"].http["url"] == expected_alive


def test_controller_container_unreachable_does_not_add_layer(
    ctx, controller_container_disconnected
):
    """If the controller container is unreachable, cert push short-circuits and no layer."""
    state_in = State(leader=True, containers=[controller_container_disconnected])
    out = ctx.run(ctx.on.install(), state_in)
    plan = get_layer(out, "lws-controller")
    assert "lws-controller" not in plan.services


def test_metrics_endpoint_relation_advertises_controller_job(ctx, base_state):
    """The metrics-endpoint relation should advertise the controller scrape job."""
    metrics_rel = Relation(endpoint="metrics-endpoint", interface="prometheus_scrape")
    state_in = dataclasses.replace(base_state, relations=[metrics_rel])

    out = ctx.run(ctx.on.relation_joined(metrics_rel), state_in)

    out_rel = out.get_relation(metrics_rel.id)
    scrape_jobs_raw = out_rel.local_app_data.get("scrape_jobs", "")
    assert f"*:{METRICS_PORT}" in scrape_jobs_raw
