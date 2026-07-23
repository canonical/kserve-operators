# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the kserve-controller and lws-controller relation gating."""

import pytest
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.testing import Relation, State

from .helpers import assert_status


def _kc_relation(data):
    return Relation(
        endpoint="kserve-controller",
        interface="kserve-controller-sync",
        remote_app_name="kserve-controller",
        remote_app_data=data,
    )


def _lws_relation(data):
    return Relation(
        endpoint="lws-controller",
        interface="lws-controller-sync",
        remote_app_name="lws-controller",
        remote_app_data=data,
    )


@pytest.mark.parametrize(
    "kc_data, expected_status, expected_msg_substr",
    [
        (None, BlockedStatus, "Please relate to kserve-controller"),
        ({}, WaitingStatus, "kserve-controller"),
        ({"ready": "false"}, WaitingStatus, "kserve-controller"),
    ],
)
def test_kserve_controller_relation_state_drives_status(
    ctx, both_containers, lws_relation_ready, kc_data, expected_status, expected_msg_substr
):
    """Charm should report Blocked/Waiting until the kserve-controller relation is ready."""
    relations = [lws_relation_ready]
    if kc_data is not None:
        relations.append(_kc_relation(kc_data))
    state_in = State(leader=True, containers=both_containers, relations=relations)
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, expected_status, expected_msg_substr)


@pytest.mark.parametrize(
    "lws_data, expected_status, expected_msg_substr",
    [
        (None, BlockedStatus, "Please relate to lws-controller"),
        ({}, WaitingStatus, "lws-controller"),
        ({"ready": "false"}, WaitingStatus, "lws-controller"),
    ],
)
def test_lws_controller_relation_state_drives_status(
    ctx, both_containers, controller_relation_ready, lws_data, expected_status, expected_msg_substr
):
    """Charm should report Blocked/Waiting until the lws-controller relation is ready."""
    relations = [controller_relation_ready]
    if lws_data is not None:
        relations.append(_lws_relation(lws_data))
    state_in = State(leader=True, containers=both_containers, relations=relations)
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, expected_status, expected_msg_substr)


def test_both_relations_ready_becomes_active(ctx, both_containers, ready_state):
    """When both relations report ready=true, the charm should become Active."""
    out = ctx.run(ctx.on.config_changed(), ready_state)
    assert_status(out, ActiveStatus)


def test_kserve_controller_relation_broken_returns_to_blocked(ctx, ready_state):
    """Breaking the kserve-controller relation must revert status to Blocked."""
    kc_rel = next(r for r in ready_state.relations if r.endpoint == "kserve-controller")
    out = ctx.run(ctx.on.relation_broken(kc_rel), ready_state)
    assert_status(out, BlockedStatus, "Please relate to kserve-controller")


def test_lws_controller_relation_broken_returns_to_blocked(ctx, ready_state):
    """Breaking the lws-controller relation must revert status to Blocked.

    kserve-controller is validated first, so the Blocked message will reference it
    only when present. With kserve-controller still ready, lws missing produces
    the lws-specific Blocked message.
    """
    lws_rel = next(r for r in ready_state.relations if r.endpoint == "lws-controller")
    out = ctx.run(ctx.on.relation_broken(lws_rel), ready_state)
    assert_status(out, BlockedStatus, "Please relate to lws-controller")


def _llm_integrator_relation():
    return Relation(
        endpoint="kserve-llmisvc",
        interface="kserve-llmisvc-sync",
        remote_app_name="llm-integrator",
    )


def test_publishes_ready_true_when_active(
    ctx, both_containers, controller_relation_ready, lws_relation_ready
):
    """When the charm becomes Active it publishes ready=true to llm-integrator."""
    llm_rel = _llm_integrator_relation()
    state_in = State(
        leader=True,
        containers=both_containers,
        relations=[controller_relation_ready, lws_relation_ready, llm_rel],
    )
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert isinstance(out.unit_status, ActiveStatus)
    assert out.get_relation(llm_rel.id).local_app_data["ready"] == "true"


def test_publishes_ready_false_when_not_active(ctx, both_containers, lws_relation_ready):
    """When the charm is not Active it publishes ready=false to llm-integrator."""
    llm_rel = _llm_integrator_relation()
    # kserve-controller relation missing -> Blocked -> ready must be false.
    state_in = State(
        leader=True,
        containers=both_containers,
        relations=[lws_relation_ready, llm_rel],
    )
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert isinstance(out.unit_status, BlockedStatus)
    assert out.get_relation(llm_rel.id).local_app_data["ready"] == "false"
