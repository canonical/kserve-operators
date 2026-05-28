# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the kserve-controller relation gating."""

import pytest
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.testing import Relation, State

from .helpers import assert_status


@pytest.mark.parametrize(
    "remote_app_data, expected_status, expected_msg_substr",
    [
        (None, BlockedStatus, "Please relate"),
        ({}, WaitingStatus, "kserve-controller"),
        ({"ready": "false"}, WaitingStatus, "kserve-controller"),
        ({"ready": "true"}, ActiveStatus, None),
    ],
)
def test_relation_state_drives_status(
    ctx, both_containers, remote_app_data, expected_status, expected_msg_substr
):
    """Charm should reach Active only when remote app sets ready=true."""
    relations = []
    if remote_app_data is not None:
        relations.append(
            Relation(
                endpoint="kserve-controller",
                interface="kserve-controller-sync",
                remote_app_name="kserve-controller",
                remote_app_data=remote_app_data,
            )
        )
    state_in = State(leader=True, containers=both_containers, relations=relations)
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, expected_status, expected_msg_substr)


def test_relation_broken_returns_to_blocked(ctx, ready_state):
    """When the kserve-controller relation is broken, status should revert to Blocked."""
    rel = next(iter(ready_state.relations))
    out = ctx.run(ctx.on.relation_broken(rel), ready_state)
    assert_status(out, BlockedStatus, "Please relate")
