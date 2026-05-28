# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for KRH (KubernetesResourceHandler) wiring and remove ordering."""

from unittest.mock import patch

import pytest
from lightkube import ApiError
from lightkube.core.exceptions import LoadResourceError
from ops.model import MaintenanceStatus
from scenario.errors import UncaughtCharmError

from .helpers import assert_status


class _FakeResponse:
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message

    def json(self):
        return {"apiVersion": 1, "code": self.code, "message": self.message}


class _FakeApiError(ApiError):
    def __init__(self, code: int, message: str):
        super().__init__(response=_FakeResponse(code, message))


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


@pytest.mark.parametrize(
    "error_message, expected_status_msg",
    [
        ("connect: connection refused", "Charm Pod is not ready yet"),
        ("no endpoints available", "Webhook Server Service endpoints not ready"),
    ],
)
def test_install_scheduler_apply_transient_error_sets_maintenance(
    ctx,
    ready_state,
    mock_krh_apply,
    error_message,
    expected_status_msg,
):
    """Known transient scheduler apply errors should keep reconcile in maintenance."""
    mock_krh_apply.side_effect = [None, _FakeApiError(500, error_message)]
    out = ctx.run(ctx.on.install(), ready_state)
    assert_status(out, MaintenanceStatus, expected_status_msg)


def test_install_scheduler_apply_unexpected_api_error_raises_runtime_error(
    ctx, ready_state, mock_krh_apply
):
    """Unexpected scheduler apply ApiError should raise GenericCharmRuntimeError."""
    mock_krh_apply.side_effect = [None, _FakeApiError(403, "forbidden")]
    with pytest.raises(UncaughtCharmError, match="GenericCharmRuntimeError"):
        ctx.run(ctx.on.install(), ready_state)


def test_install_base_apply_api_error_is_re_raised(ctx, ready_state, mock_krh_apply):
    """ApiError outside scheduler apply block should bubble up from _on_event."""
    mock_krh_apply.side_effect = _FakeApiError(500, "internal error")
    with pytest.raises(UncaughtCharmError, match="_FakeApiError"):
        ctx.run(ctx.on.install(), ready_state)


def test_remove_ignores_404_delete_errors(ctx, ready_state, mock_krh_delete):
    """404 on delete should be ignored so remove can finish."""
    mock_krh_delete.side_effect = _FakeApiError(404, "not found")
    out = ctx.run(ctx.on.remove(), ready_state)
    assert_status(out, MaintenanceStatus, "resources removed")


def test_remove_non_404_delete_errors_raise(ctx, ready_state, mock_krh_delete):
    """Non-404 delete errors are unexpected and should be raised."""
    mock_krh_delete.side_effect = _FakeApiError(500, "internal")
    with pytest.raises(UncaughtCharmError, match="_FakeApiError"):
        ctx.run(ctx.on.remove(), ready_state)
