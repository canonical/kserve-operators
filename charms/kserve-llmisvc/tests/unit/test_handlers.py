# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for KRH (KubernetesResourceHandler) wiring and remove ordering."""

from unittest.mock import patch

from lightkube.core.exceptions import LoadResourceError
from ops.model import MaintenanceStatus

from .helpers import assert_status


def test_two_handlers_applied_on_install(ctx, ready_state, mock_krh_apply):
    """install_or_upgrade should apply base and scheduler-config handlers."""
    ctx.run(ctx.on.install(), ready_state)
    # mock_krh_apply is patched onto KubernetesResourceHandler.apply, so each
    # call corresponds to one handler.apply().
    assert mock_krh_apply.call_count >= 2


def test_remove_calls_delete_for_each_handler(ctx, ready_state, mock_krh_apply, mock_krh_delete):
    """on_remove should call delete on each handler whose sync produced manifests."""
    out = ctx.run(ctx.on.remove(), ready_state)
    assert_status(out, MaintenanceStatus, "resources removed")
    # 2 handlers (BASE, SCHEDULER_CONFIG) each get a delete attempt.
    assert mock_krh_delete.call_count == 2


def test_remove_skips_delete_when_no_manifests(ctx, ready_state, mock_krh_delete):
    """When render returns no manifests, delete should be skipped for that handler."""
    with patch("charm.KubernetesResourceHandler.render_manifests", return_value=[]):
        ctx.run(ctx.on.remove(), ready_state)
    mock_krh_delete.assert_not_called()


def test_sync_handler_resource_types_handles_load_resource_error(ctx, ready_state):
    """A LoadResourceError during sync should be caught and logged, not propagated."""
    with patch(
        "charm.KubernetesResourceHandler.render_manifests",
        side_effect=LoadResourceError("missing CRD"),
    ):
        # Must not raise even though render fails.
        out = ctx.run(ctx.on.remove(), ready_state)
    assert_status(out, MaintenanceStatus, "resources removed")
