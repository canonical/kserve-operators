# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Prometheus metrics-endpoint assertions."""

import json
from dataclasses import replace

from ops.testing import Relation

from charm import (
    ADAPTER_METRICS_PORT,
    METRICS_APISERVER_CONTAINER,
    OPERATOR_CONTAINER,
    OPERATOR_METRICS_PORT,
    WEBHOOK_METRICS_PORT,
    WEBHOOKS_CONTAINER,
)

from .helpers import get_layer


def test_metrics_endpoint_publishes_all_component_ports(ctx, base_state):
    """The scrape jobs must cover the operator, adapter and webhook /metrics ports."""
    relation = Relation(endpoint="metrics-endpoint", interface="prometheus_scrape")
    state = replace(base_state, relations={relation})

    out = ctx.run(ctx.on.relation_joined(relation), state)

    out_relation = next(r for r in out.relations if r.endpoint == "metrics-endpoint")
    scrape_jobs = json.loads(out_relation.local_app_data["scrape_jobs"])
    targets = {
        target
        for job in scrape_jobs
        for config in job["static_configs"]
        for target in config["targets"]
    }
    assert f"*:{OPERATOR_METRICS_PORT}" in targets
    assert f"*:{ADAPTER_METRICS_PORT}" in targets
    assert f"*:{WEBHOOK_METRICS_PORT}" in targets


def test_logging_relation_forwards_all_container_logs(ctx, base_state):
    """A Loki logging relation must add a Pebble log-target to every container."""
    relation = Relation(
        endpoint="logging",
        interface="loki_push_api",
        remote_app_name="loki",
        remote_units_data={
            0: {"endpoint": json.dumps({"url": "http://loki:3100/loki/api/v1/push"})}
        },
    )
    state = replace(base_state, relations={relation})

    out = ctx.run(ctx.on.relation_changed(relation), state)

    for name in (OPERATOR_CONTAINER, METRICS_APISERVER_CONTAINER, WEBHOOKS_CONTAINER):
        assert get_layer(out, name).log_targets, f"no log-target configured for {name}"
