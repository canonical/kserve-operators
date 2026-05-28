# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for custom-image config parsing and merging."""

import pytest
import yaml
from ops.model import ActiveStatus, BlockedStatus
from ops.testing import State

from charm import KServeLLMISVCCharm, parse_images_config

from .helpers import assert_status, build_custom_images_config

# ---------------------------------------------------------------------------
# Pure-function parse_images_config tests.
# ---------------------------------------------------------------------------


def test_parse_images_config_empty_string_returns_empty():
    assert parse_images_config("") == {}


def test_parse_images_config_null_yaml_returns_empty():
    assert parse_images_config("null") == {}


def test_parse_images_config_valid_yaml_returns_dict():
    result = parse_images_config("llm_scheduler: foo/scheduler:1")
    assert result == {"llm_scheduler": "foo/scheduler:1"}


def test_parse_images_config_routing_sidecar_key_returns_dict():
    result = parse_images_config("llm_routing_sidecar: foo/routing-sidecar:2")
    assert result == {"llm_routing_sidecar": "foo/routing-sidecar:2"}


def test_parse_images_config_invalid_yaml_raises_blocked():
    from charmed_kubeflow_chisme.exceptions import ErrorWithStatus

    with pytest.raises(ErrorWithStatus) as ei:
        parse_images_config("foo: [unclosed")
    assert ei.value.status_type is BlockedStatus


def test_parse_images_config_non_dict_raises_blocked():
    from charmed_kubeflow_chisme.exceptions import ErrorWithStatus

    with pytest.raises(ErrorWithStatus) as ei:
        parse_images_config("- a\n- b")
    assert ei.value.status_type is BlockedStatus


# ---------------------------------------------------------------------------
# get_images merge behaviour (invoked indirectly via config-changed).
# ---------------------------------------------------------------------------


def test_invalid_custom_images_sets_blocked(ctx, both_containers, controller_relation_ready):
    """Invalid YAML in custom_images should produce BlockedStatus."""
    state_in = State(
        leader=True,
        containers=both_containers,
        relations=[controller_relation_ready],
        config={"custom_images": "foo: [unclosed"},
    )
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, BlockedStatus)


def test_valid_custom_images_keeps_active(ctx, both_containers, controller_relation_ready):
    """A valid override for a known image key should keep the charm Active."""
    state_in = State(
        leader=True,
        containers=both_containers,
        relations=[controller_relation_ready],
        config={
            "custom_images": build_custom_images_config(llm_scheduler="example.com/scheduler:dev")
        },
    )
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, ActiveStatus)


def test_get_images_merges_routing_sidecar_override():
    merged = KServeLLMISVCCharm.get_images(
        None,
        default_images={"llm_routing_sidecar": "ghcr.io/default:1"},
        custom_images={"llm_routing_sidecar": "ghcr.io/override:2"},
    )
    assert merged["llm_routing_sidecar"] == "ghcr.io/override:2"


def test_unknown_custom_image_key_warns_but_active(
    ctx, both_containers, controller_relation_ready, caplog
):
    """Unknown keys in custom_images should be logged as warnings and ignored."""
    state_in = State(
        leader=True,
        containers=both_containers,
        relations=[controller_relation_ready],
        config={"custom_images": yaml.safe_dump({"not_a_real_image": "foo:bar"})},
    )
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, ActiveStatus)
    assert any("not_a_real_image" in rec.message for rec in caplog.records)
