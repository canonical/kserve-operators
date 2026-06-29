# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for KRH (KubernetesResourceHandler) wiring and remove ordering."""

from unittest.mock import MagicMock, patch

import pytest
from lightkube import ApiError
from lightkube.core.exceptions import LoadResourceError
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from ops.model import MaintenanceStatus
from scenario.errors import UncaughtCharmError

from charm import ObjectStillExistsError

from .helpers import assert_status


def _fake_crd(name="leaderworkersets.leaderworkerset.x-k8s.io"):
    """A manifest that passes ``isinstance(..., CustomResourceDefinition)``."""
    crd = MagicMock(spec=CustomResourceDefinition)
    crd.metadata.name = name
    crd.metadata.namespace = None
    return crd


def _fake_non_crd(name="lws-webhook"):
    """A non-CRD base manifest (e.g. webhook/RBAC)."""
    resource = MagicMock()
    resource.metadata.name = name
    resource.metadata.namespace = "kubeflow"
    return resource


class _FakeResponse:
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message

    def json(self):
        return {"apiVersion": 1, "code": self.code, "message": self.message}


class _FakeApiError(ApiError):
    def __init__(self, code: int, message: str):
        super().__init__(response=_FakeResponse(code, message))


def test_base_handler_applied_on_install(ctx, base_state, mock_krh_apply):
    """install should apply the base handler exactly once."""
    ctx.run(ctx.on.install(), base_state)
    assert mock_krh_apply.call_count >= 1


def test_remove_calls_delete_for_base_handler(ctx, base_state, mock_krh_delete):
    """on_remove should call delete on the base handler when sync produced manifests."""
    out = ctx.run(ctx.on.remove(), base_state)
    assert_status(out, MaintenanceStatus, "resources removed")
    assert mock_krh_delete.call_count == 1


def test_remove_skips_delete_when_no_manifests(ctx, base_state, mock_krh_delete):
    """When render returns no manifests, delete should be skipped."""
    with patch("charm.KubernetesResourceHandler.render_manifests", return_value=[]):
        ctx.run(ctx.on.remove(), base_state)
    mock_krh_delete.assert_not_called()


def test_sync_handler_resource_types_handles_load_resource_error(ctx, base_state):
    """A LoadResourceError during sync should be caught and logged, not propagated."""
    with patch(
        "charm.KubernetesResourceHandler.render_manifests",
        side_effect=LoadResourceError("missing CRD"),
    ):
        out = ctx.run(ctx.on.remove(), base_state)
    assert_status(out, MaintenanceStatus, "resources removed")


def test_install_base_apply_api_error_is_re_raised(ctx, base_state, mock_krh_apply):
    """ApiError during apply should bubble up from _on_event."""
    mock_krh_apply.side_effect = _FakeApiError(500, "internal error")
    with pytest.raises(UncaughtCharmError, match="_FakeApiError"):
        ctx.run(ctx.on.install(), base_state)


def test_remove_ignores_404_delete_errors(ctx, base_state, mock_krh_delete):
    """404 on delete should be ignored so remove can finish."""
    mock_krh_delete.side_effect = _FakeApiError(404, "not found")
    out = ctx.run(ctx.on.remove(), base_state)
    assert_status(out, MaintenanceStatus, "resources removed")


def test_remove_non_404_delete_errors_raise(ctx, base_state, mock_krh_delete):
    """Non-404 delete errors are unexpected and should be raised."""
    mock_krh_delete.side_effect = _FakeApiError(500, "internal")
    with pytest.raises(UncaughtCharmError, match="_FakeApiError"):
        ctx.run(ctx.on.remove(), base_state)


def test_remove_deletes_crd_before_base_resources(ctx, base_state, mock_krh_delete):
    """CRDs are deleted (and waited on) first, then the base handler delete runs."""
    crd = _fake_crd()
    webhook = _fake_non_crd()
    with patch("charm.delete_many") as mock_delete_many, patch(
        "charm.KubernetesResourceHandler.render_manifests", return_value=[crd, webhook]
    ):
        out = ctx.run(ctx.on.remove(), base_state)
    # The CRD goes through delete_many; the rest go through the handler delete.
    mock_delete_many.assert_called_once()
    assert mock_delete_many.call_args.args[1] == [crd]
    mock_krh_delete.assert_called_once()
    assert_status(out, MaintenanceStatus, "resources removed")


def test_remove_raises_when_resource_stuck(ctx, base_state, mock_krh_delete):
    """A resource that never disappears surfaces ObjectStillExistsError."""
    crd = _fake_crd()
    with patch("charm.delete_many"), patch(
        "charm.KubernetesResourceHandler.render_manifests", return_value=[crd]
    ), patch(
        "charm.LWSControllerCharm.ensure_resource_is_deleted",
        side_effect=ObjectStillExistsError(crd.metadata.name),
    ):
        with pytest.raises(UncaughtCharmError, match="ObjectStillExistsError"):
            ctx.run(ctx.on.remove(), base_state)


def test_remove_crd_delete_many_error_raises(ctx, base_state):
    """A non-404 ApiError from the CRD delete_many bubbles up."""
    crd = _fake_crd()
    with patch("charm.delete_many", side_effect=_FakeApiError(500, "internal")), patch(
        "charm.KubernetesResourceHandler.render_manifests", return_value=[crd]
    ):
        with pytest.raises(UncaughtCharmError, match="_FakeApiError"):
            ctx.run(ctx.on.remove(), base_state)
