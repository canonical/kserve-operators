# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Readiness relation and reconcile status assertions."""

from dataclasses import replace

from ops.model import ActiveStatus

from charm import KEDA_SYNC_RELATION


def test_reconcile_sets_active_and_publishes_ready(ctx, base_state):
    """A successful reconcile leaves the unit Active."""
    out = ctx.run(ctx.on.install(), base_state)
    assert isinstance(out.unit_status, ActiveStatus)


def test_publishes_ready_true_on_relation(ctx, base_state, consumer_relation):
    """The leader publishes ready=true on the keda relation after reconcile."""
    state_in = replace(base_state, relations=[consumer_relation])
    out = ctx.run(ctx.on.relation_changed(consumer_relation), state_in)

    relation = out.get_relation(consumer_relation.id)
    assert relation.local_app_data.get("ready") == "true"
    assert relation.local_app_data.get("namespace")


def test_relation_endpoint_name():
    """Guard the relation name the readiness contract is published on."""
    assert KEDA_SYNC_RELATION == "keda"
