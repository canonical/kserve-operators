# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Certificate generation and propagation tests."""

from ops.testing import State


def test_gen_certs_invoked_on_install(ctx, base_state, mock_gen_certs):
    """gen_certs should be invoked at least once during install reconcile."""
    ctx.run(ctx.on.install(), base_state)
    assert mock_gen_certs.called


def test_certs_pushed_to_controller_container(ctx, base_state):
    """The three cert files are pushed into the controller container filesystem."""
    out = ctx.run(ctx.on.install(), base_state)
    controller = out.get_container("lws-controller")
    fs = controller.get_filesystem(ctx)
    cert_dir = fs / "tmp" / "k8s-webhook-server" / "serving-certs"
    assert (cert_dir / "tls.key").read_text() == "k"
    assert (cert_dir / "tls.crt").read_text() == "c"
    assert (cert_dir / "ca.crt").read_text() == "a"


def test_manager_config_pushed_to_controller_container(ctx, base_state):
    """The LWS controller manager configuration file should be pushed to the container."""
    out = ctx.run(ctx.on.install(), base_state)
    controller = out.get_container("lws-controller")
    fs = controller.get_filesystem(ctx)
    config_file = fs / "tmp" / "lws" / "controller_manager_config.yaml"
    content = config_file.read_text()
    assert "internalCertManagement" in content
    assert "enable: false" in content


def test_certs_skipped_when_container_unreachable(
    ctx, base_state, controller_container_disconnected
):
    """When the controller container can't be reached, push must be skipped (no raise)."""
    state_in = State(leader=True, containers=[controller_container_disconnected])

    # Reconcile must complete without raising even though the container is unreachable.
    ctx.run(ctx.on.install(), state_in)
