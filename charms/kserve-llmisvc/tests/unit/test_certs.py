# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Certificate generation and propagation tests."""

import dataclasses


def test_gen_certs_invoked_on_install(ctx, ready_state, mock_gen_certs):
    """gen_certs should be invoked at least once during install reconcile."""
    ctx.run(ctx.on.install(), ready_state)
    assert mock_gen_certs.called


def test_certs_pushed_to_controller_container(ctx, ready_state):
    """The three cert files are pushed into the controller container filesystem."""
    out = ctx.run(ctx.on.install(), ready_state)
    controller = out.get_container("llmisvc-controller")
    fs = controller.get_filesystem(ctx)
    cert_dir = fs / "tmp" / "k8s-webhook-server" / "serving-certs"
    assert (cert_dir / "tls.key").read_text() == "k"
    assert (cert_dir / "tls.crt").read_text() == "c"
    assert (cert_dir / "ca.crt").read_text() == "a"


def test_certs_skipped_when_container_unreachable(
    ctx,
    base_state,
    controller_relation_ready,
    lws_relation_ready,
    controller_container_disconnected,
):
    """When the controller container can't be reached, push must be skipped (no raise)."""
    containers = [c for c in base_state.containers if c.name != "llmisvc-controller"]
    containers.append(controller_container_disconnected)
    state_in = dataclasses.replace(
        base_state,
        containers=containers,
        relations=[controller_relation_ready, lws_relation_ready],
    )

    # Reconcile must complete without raising even though the container is unreachable.
    ctx.run(ctx.on.install(), state_in)
