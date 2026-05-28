# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Lifecycle smoke tests for the kserve-llmisvc charm.

These cover the top-level reconcile flow only. More focused suites live in
sibling files (``test_handlers.py``, ``test_pebble.py``, ...).
"""

import dataclasses

from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

from .helpers import assert_status


def test_install_without_relation_blocks(ctx, base_state):
    """Without the kserve-controller relation, the unit should block for user action."""
    out = ctx.run(ctx.on.install(), base_state)
    assert_status(out, BlockedStatus, msg_substr="Please relate")


def test_install_with_relation_ready_becomes_active(ctx, ready_state):
    """When the controller relation reports ready=true, reconcile succeeds."""
    out = ctx.run(ctx.on.install(), ready_state)
    assert_status(out, ActiveStatus)


def test_install_with_relation_missing_ready_key_waits(
    ctx, base_state, controller_relation_not_ready
):
    """A controller relation without ready=true should keep the unit waiting."""
    state_in = dataclasses.replace(base_state, relations=[controller_relation_not_ready])
    out = ctx.run(ctx.on.install(), state_in)
    assert_status(out, WaitingStatus)


def test_config_changed_becomes_active(ctx, ready_state):
    """config-changed runs the same reconcile loop and becomes active when ready."""
    out = ctx.run(ctx.on.config_changed(), ready_state)
    assert_status(out, ActiveStatus)


def test_remove_sets_maintenance_status(ctx, ready_state):
    """The remove hook should finish in MaintenanceStatus."""
    out = ctx.run(ctx.on.remove(), ready_state)
    assert_status(out, MaintenanceStatus, msg_substr="resources removed")
