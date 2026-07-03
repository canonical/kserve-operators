# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the lws-controller readiness contract published over the relation."""

import dataclasses

from ops.model import ActiveStatus

from charm import LWS_SYNC_RELATION


def test_relation_publishes_ready_true_after_successful_reconcile(
    ctx, base_state, consumer_relation
):
    """A successful reconcile should publish ready=true on every consumer relation."""
    state_in = dataclasses.replace(base_state, relations=[consumer_relation])
    out = ctx.run(ctx.on.config_changed(), state_in)

    out_rel = out.get_relation(consumer_relation.id)
    assert out_rel.local_app_data.get("ready") == "true"
    # ``namespace`` is rendered from ``model.name``; assert the key exists and is non-empty.
    assert out_rel.local_app_data.get("namespace")


def test_relation_broken_does_not_block_charm(ctx, base_state, consumer_relation):
    """Breaking the consumer relation should not cause the lws-controller to block."""
    state_in = dataclasses.replace(base_state, relations=[consumer_relation])
    out = ctx.run(ctx.on.relation_broken(consumer_relation), state_in)

    assert isinstance(out.unit_status, ActiveStatus)


def test_non_leader_does_not_publish_relation_data(ctx, base_state, consumer_relation):
    """Only the leader unit should write to the relation app data."""
    state_in = dataclasses.replace(base_state, leader=False, relations=[consumer_relation])
    out = ctx.run(ctx.on.config_changed(), state_in)

    out_rel = out.get_relation(consumer_relation.id)
    assert "ready" not in out_rel.local_app_data


def test_relation_endpoint_name_matches_metadata():
    """Regression: the publish endpoint name must match the metadata.yaml entry."""
    assert LWS_SYNC_RELATION == "lws-controller"
