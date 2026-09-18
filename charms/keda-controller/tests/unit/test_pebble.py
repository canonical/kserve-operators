# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Pebble layer assertions for the three keda containers."""

from dataclasses import replace

from ops.model import BlockedStatus

from charm import (
    ADAPTER_METRICS_PORT,
    METRICS_APISERVER_CONTAINER,
    OPERATOR_CONTAINER,
    OPERATOR_HEALTH_PORT,
    WEBHOOK_HEALTH_PORT,
    WEBHOOKS_CONTAINER,
)

from .helpers import get_layer

ALL_CONTAINERS = (OPERATOR_CONTAINER, METRICS_APISERVER_CONTAINER, WEBHOOKS_CONTAINER)


def test_all_three_services_present(ctx, base_state):
    """All three KEDA workloads should be defined and enabled on install."""
    out = ctx.run(ctx.on.install(), base_state)
    for name in ALL_CONTAINERS:
        svc = get_layer(out, name).services[name]
        assert svc.startup == "enabled"


def test_operator_layer(ctx, base_state):
    """The operator runs /keda with cert-rotation disabled and the configured log level."""
    out = ctx.run(ctx.on.install(), base_state)
    svc = get_layer(out, OPERATOR_CONTAINER).services[OPERATOR_CONTAINER]

    assert "/keda " in svc.command
    assert "--leader-elect" in svc.command
    assert "--enable-cert-rotation=false" in svc.command
    assert "--zap-log-level=info" in svc.command
    assert svc.environment["POD_NAMESPACE"]
    assert "WATCH_NAMESPACE" in svc.environment


def test_metrics_apiserver_layer(ctx, base_state):
    """The adapter serves on 6443, dials the operator gRPC, and uses a distinct metrics port."""
    out = ctx.run(ctx.on.install(), base_state)
    svc = get_layer(out, METRICS_APISERVER_CONTAINER).services[METRICS_APISERVER_CONTAINER]

    assert "/keda-adapter" in svc.command
    assert "--secure-port=6443" in svc.command
    assert "--metrics-service-address=127.0.0.1:9666" in svc.command
    assert f"--port={ADAPTER_METRICS_PORT}" in svc.command
    assert "--tls-cert-file=/certs/tls.crt" in svc.command
    assert "--client-ca-file=/certs/ca.crt" in svc.command


def test_webhooks_layer(ctx, base_state):
    """The webhooks server reads its cert from /certs and uses non-colliding ports."""
    out = ctx.run(ctx.on.install(), base_state)
    svc = get_layer(out, WEBHOOKS_CONTAINER).services[WEBHOOKS_CONTAINER]

    assert "/keda-admission-webhooks" in svc.command
    assert "--cert-dir=/certs" in svc.command
    assert f"--health-probe-bind-address=:{WEBHOOK_HEALTH_PORT}" in svc.command


def test_health_checks_use_distinct_ports(ctx, base_state):
    """Operator and webhook readiness checks must target different ports (shared pod netns)."""
    out = ctx.run(ctx.on.install(), base_state)
    op_checks = get_layer(out, OPERATOR_CONTAINER).checks
    wh_checks = get_layer(out, WEBHOOKS_CONTAINER).checks

    assert op_checks["operator-ready"].http["url"] == (
        f"http://localhost:{OPERATOR_HEALTH_PORT}/readyz"
    )
    assert wh_checks["webhooks-ready"].http["url"] == (
        f"http://localhost:{WEBHOOK_HEALTH_PORT}/readyz"
    )
    assert OPERATOR_HEALTH_PORT != WEBHOOK_HEALTH_PORT


def test_log_level_config_propagates(ctx, base_state):
    """The log-level config option flows into the operator zap flag."""
    state_in = replace(base_state, config={"log-level": "debug"})
    out = ctx.run(ctx.on.config_changed(), state_in)
    svc = get_layer(out, OPERATOR_CONTAINER).services[OPERATOR_CONTAINER]
    assert "--zap-log-level=debug" in svc.command


def test_log_level_accepts_positive_integer(ctx, base_state):
    """KEDA/zap also accepts a positive integer verbosity level."""
    state_in = replace(base_state, config={"log-level": "2"})
    out = ctx.run(ctx.on.config_changed(), state_in)
    svc = get_layer(out, OPERATOR_CONTAINER).services[OPERATOR_CONTAINER]
    assert "--zap-log-level=2" in svc.command


def test_invalid_log_level_blocks(ctx, base_state):
    """An unrecognised log-level must block the unit instead of crashing the workload."""
    state_in = replace(base_state, config={"log-level": "bogus"})
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert isinstance(out.unit_status, BlockedStatus)


def test_watch_namespace_config_propagates_to_all_containers(ctx, base_state):
    """A configured watch-namespace must reach every container's WATCH_NAMESPACE env."""
    state_in = replace(base_state, config={"watch-namespace": "team-a"})
    out = ctx.run(ctx.on.config_changed(), state_in)
    for name in ALL_CONTAINERS:
        svc = get_layer(out, name).services[name]
        assert svc.environment["WATCH_NAMESPACE"] == "team-a"


def test_watch_namespace_defaults_to_cluster_wide(ctx, base_state):
    """With no config, WATCH_NAMESPACE is empty (cluster-wide)."""
    out = ctx.run(ctx.on.install(), base_state)
    svc = get_layer(out, OPERATOR_CONTAINER).services[OPERATOR_CONTAINER]
    assert svc.environment["WATCH_NAMESPACE"] == ""
