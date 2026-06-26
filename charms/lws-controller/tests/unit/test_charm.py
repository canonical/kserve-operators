# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Lifecycle smoke tests for the lws-controller charm."""

from ops.model import ActiveStatus, MaintenanceStatus

from .helpers import assert_status


def test_install_without_relation_becomes_active(ctx, base_state):
    """The lws-controller has no required relations and should become Active on install."""
    out = ctx.run(ctx.on.install(), base_state)
    assert_status(out, ActiveStatus)


def test_config_changed_becomes_active(ctx, base_state):
    """config-changed runs the same reconcile loop and becomes active."""
    out = ctx.run(ctx.on.config_changed(), base_state)
    assert_status(out, ActiveStatus)


def test_remove_sets_maintenance_status(ctx, base_state):
    """The remove hook should finish in MaintenanceStatus."""
    out = ctx.run(ctx.on.remove(), base_state)
    assert_status(out, MaintenanceStatus, msg_substr="resources removed")
