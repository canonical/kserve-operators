# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Certificate generation and propagation tests."""

import dataclasses

from ops.testing import StoredState

STORED_CERTS = {"cert": "old-c", "key": "old-k", "ca": "old-a"}


def _with_stored_certs(state):
    stored = StoredState(owner_path="KServeLLMISVCCharm", name="_stored", content=STORED_CERTS)
    return dataclasses.replace(state, stored_states=[stored])


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


def test_valid_stored_certs_are_reused(ctx, ready_state, mock_gen_certs):
    """Certificates already in stored state and not expiring must not be regenerated."""
    out = ctx.run(ctx.on.update_status(), _with_stored_certs(ready_state))
    assert not mock_gen_certs.called
    controller = out.get_container("llmisvc-controller")
    cert_dir = controller.get_filesystem(ctx) / "tmp" / "k8s-webhook-server" / "serving-certs"
    assert (cert_dir / "tls.crt").read_text() == "old-c"


def test_expiring_certs_are_renewed_and_pushed(
    ctx, ready_state, mock_gen_certs, mock_cert_expires_soon
):
    """An expiring serving certificate is regenerated and the new certs pushed."""
    mock_cert_expires_soon.return_value = True
    out = ctx.run(ctx.on.update_status(), _with_stored_certs(ready_state))
    mock_cert_expires_soon.assert_called_with("old-c")
    assert mock_gen_certs.called
    controller = out.get_container("llmisvc-controller")
    cert_dir = controller.get_filesystem(ctx) / "tmp" / "k8s-webhook-server" / "serving-certs"
    assert (cert_dir / "tls.crt").read_text() == "c"
    assert (cert_dir / "tls.key").read_text() == "k"
    assert (cert_dir / "ca.crt").read_text() == "a"
