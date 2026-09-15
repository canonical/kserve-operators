# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Reconcile and removal lifecycle assertions."""

from base64 import b64encode

from ops.model import MaintenanceStatus

from charm import ADAPTER_METRICS_PORT, OPERATOR_CONTAINER, WEBHOOK_METRICS_PORT

from .helpers import get_layer


def test_reconcile_applies_base_resources(ctx, base_state, mock_krh_apply):
    """A reconcile renders and applies the KEDA CRDs/RBAC/Services/APIService/webhooks."""
    ctx.run(ctx.on.install(), base_state)
    mock_krh_apply.assert_called()


def test_disconnected_container_blocks_and_skips_layers(ctx, state_operator_disconnected):
    """If a container is unreachable, cert push short-circuits to Maintenance and adds no layer."""
    out = ctx.run(ctx.on.install(), state_operator_disconnected)
    assert isinstance(out.unit_status, MaintenanceStatus)
    assert OPERATOR_CONTAINER not in get_layer(out, OPERATOR_CONTAINER).services


def test_remove_deletes_base_resources(ctx, base_state, mock_krh_delete):
    """Removing the charm deletes the resources it rendered (incl. the APIService)."""
    ctx.run(ctx.on.remove(), base_state)
    mock_krh_delete.assert_called()


def test_context_carries_app_namespace_and_ca_bundle(ctx, base_state):
    """The template context exposes app/namespace and a base64-encoded CA bundle."""
    with ctx(ctx.on.install(), base_state) as manager:
        charm = manager.charm
        context = charm._context

    assert context["app_name"] == charm.app.name
    assert context["namespace"] == charm.model.name
    # gen_certs is mocked to return ca="a"; the bundle is the quoted base64 of it.
    expected = f"'{b64encode(b'a').decode()}'"
    assert context["cert"] == expected
    # Service metrics targetPorts must match the containers' reassigned ports.
    assert context["adapter_metrics_port"] == ADAPTER_METRICS_PORT
    assert context["webhook_metrics_port"] == WEBHOOK_METRICS_PORT


def test_certs_pushed_to_all_containers(ctx, base_state):
    """The shared serving cert bundle is written into every container's /certs dir."""
    out = ctx.run(ctx.on.install(), base_state)
    for container in out.containers:
        fs = container.get_filesystem(ctx)
        certs_dir = fs / "certs"
        assert (certs_dir / "tls.crt").exists()
        assert (certs_dir / "tls.key").exists()
        assert (certs_dir / "ca.crt").exists()
