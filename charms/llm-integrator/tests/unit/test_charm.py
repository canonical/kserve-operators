# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the llm-integrator charm reconcile and cleanup behaviour."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from ops.testing import Secret, State

from .helpers import assert_status


def _ready_condition(status: str = "True", message: str = "", reason: str = ""):
    """Return a fake LLMInferenceService object with the given Ready condition."""
    condition = {"type": "Ready", "status": status}
    if message:
        condition["message"] = message
    if reason:
        condition["reason"] = reason
    return SimpleNamespace(status={"conditions": [condition]})


def test_no_relation_blocks(ctx, valid_config):
    """Without the kserve-llmisvc relation the charm is Blocked."""
    state_in = State(leader=True, config=valid_config, relations=[])
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, BlockedStatus, "Please relate to kserve-llmisvc")


def test_relation_not_ready_waits(ctx, valid_config, llmisvc_relation_not_ready):
    """A present-but-not-ready relation puts the charm in Waiting."""
    state_in = State(leader=True, config=valid_config, relations=[llmisvc_relation_not_ready])
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, WaitingStatus, "kserve-llmisvc")


@pytest.mark.parametrize(
    "config, expected_msg",
    [
        ({}, "model-uri"),
        ({"model-uri": "gs://bucket/model", "runtime-image": "img:latest"}, "must start with"),
        ({"model-uri": "hf://x"}, "runtime-image"),
        ({"model-uri": "s3://bucket/model"}, "runtime-image"),
    ],
)
def test_invalid_config_blocks(ctx, llmisvc_relation_ready, config, expected_msg):
    """Invalid or incomplete configuration blocks the charm with a helpful message."""
    state_in = State(leader=True, config=config, relations=[llmisvc_relation_ready])
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, BlockedStatus, expected_msg)


def test_model_name_defaults_to_uri_path(ctx, llmisvc_relation_ready):
    """When model-name is unset it is derived from the hf:// URI."""
    config = {"model-uri": "hf://EleutherAI/pythia-70m", "runtime-image": "img:latest"}
    state_in = State(leader=True, config=config, relations=[llmisvc_relation_ready])
    with ctx(ctx.on.config_changed(), state_in) as manager:
        manager.run()
        assert manager.charm._context["model_name"] == "EleutherAI/pythia-70m"


def test_s3_uri_without_relation_blocks(ctx, llmisvc_relation_ready, s3_config):
    """An s3:// URI without the s3-credentials relation blocks the charm."""
    state_in = State(leader=True, config=s3_config, relations=[llmisvc_relation_ready])
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, BlockedStatus, "s3-integrator")


def test_s3_relation_without_creds_waits(
    ctx, llmisvc_relation_ready, s3_config, s3_relation, mock_s3_connection_info
):
    """An s3-credentials relation with no usable credentials yet -> Waiting."""
    mock_s3_connection_info.return_value = {}
    state_in = State(
        leader=True,
        config=s3_config,
        relations=[llmisvc_relation_ready, s3_relation],
    )
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, WaitingStatus, "s3-credentials relation data")


def test_s3_with_creds_applies(
    ctx,
    llmisvc_relation_ready,
    s3_config,
    s3_relation,
    mock_s3_connection_info,
    mock_krh_apply,
):
    """s3:// URI + relation + credentials -> apply the manifest (CR not ready yet)."""
    state_in = State(
        leader=True,
        config=s3_config,
        relations=[llmisvc_relation_ready, s3_relation],
    )
    out = ctx.run(ctx.on.config_changed(), state_in)

    mock_krh_apply.assert_called_once()
    assert_status(out, WaitingStatus, "to be created")


def test_s3_context_maps_credentials(
    ctx, llmisvc_relation_ready, s3_config, s3_relation, mock_s3_connection_info
):
    """The s3 render context derives the model name and S3 connection fields."""
    state_in = State(
        leader=True,
        config=s3_config,
        relations=[llmisvc_relation_ready, s3_relation],
    )
    with ctx(ctx.on.config_changed(), state_in) as manager:
        manager.run()
        context = manager.charm._context

    assert context["is_s3"] is True
    assert context["model_name"] == "pythia-70m"
    assert context["s3_secret_name"] == "llm-integrator-s3-creds"
    assert context["s3_access_key"] == "AKIAEXAMPLE"
    assert context["s3_secret_access_key"] == "secretexample"
    assert context["s3_endpoint"] == "s3.eu-central-1.amazonaws.com"
    assert context["s3_use_https"] == "1"
    assert context["s3_region"] == "eu-central-1"
    assert context["storage_initializer_image"]


def test_hf_token_context_maps_secret(ctx, llmisvc_relation_ready, hf_token_secret):
    """A granted hf-token-secret enables the token path and exposes render fields."""
    config = {
        "model-uri": "hf://google/gemma-3-270m-it",
        "runtime-image": "img:latest",
        "storage-initializer-image": "si:img",
        "hf-token-secret": hf_token_secret.id,
    }
    state_in = State(
        leader=True,
        config=config,
        relations=[llmisvc_relation_ready],
        secrets=[hf_token_secret],
    )
    with ctx(ctx.on.config_changed(), state_in) as manager:
        manager.run()
        context = manager.charm._context

    assert context["use_hf_token"] is True
    assert context["is_s3"] is False
    assert context["hf_secret_name"] == "llm-integrator-hf-token"
    assert context["hf_token"] == "hf_secrettoken"
    assert context["storage_initializer_image"] == "si:img"


def test_hf_public_model_needs_no_token(ctx, ready_state):
    """A public hf:// model without a token does not enable the token path."""
    with ctx(ctx.on.config_changed(), ready_state) as manager:
        manager.run()
        assert manager.charm._context["use_hf_token"] is False


def test_secret_changed_for_configured_token_reconciles(
    ctx, llmisvc_relation_ready, hf_token_secret, mock_krh_apply
):
    """A secret-changed for the configured token secret triggers a reconcile."""
    config = {
        "model-uri": "hf://google/gemma-3-270m-it",
        "runtime-image": "img:latest",
        "storage-initializer-image": "si:img",
        "hf-token-secret": hf_token_secret.id,
    }
    state_in = State(
        leader=True,
        config=config,
        relations=[llmisvc_relation_ready],
        secrets=[hf_token_secret],
    )
    ctx.run(ctx.on.secret_changed(hf_token_secret), state_in)
    mock_krh_apply.assert_called_once()


def test_secret_changed_for_unrelated_secret_ignored(
    ctx, llmisvc_relation_ready, hf_token_secret, mock_krh_apply
):
    """A secret-changed for a secret the charm does not consume is ignored."""
    other_secret = Secret(tracked_content={"token": "unrelated"})
    config = {
        "model-uri": "hf://google/gemma-3-270m-it",
        "runtime-image": "img:latest",
        "storage-initializer-image": "si:img",
        "hf-token-secret": hf_token_secret.id,
    }
    state_in = State(
        leader=True,
        config=config,
        relations=[llmisvc_relation_ready],
        secrets=[hf_token_secret, other_secret],
    )
    ctx.run(ctx.on.secret_changed(other_secret), state_in)
    mock_krh_apply.assert_not_called()


def test_hf_token_missing_storage_initializer_image_blocks(
    ctx, llmisvc_relation_ready, hf_token_secret
):
    """A gated hf:// model requires the storage-initializer image."""
    config = {
        "model-uri": "hf://google/gemma-3-270m-it",
        "runtime-image": "img:latest",
        "storage-initializer-image": "",
        "hf-token-secret": hf_token_secret.id,
    }
    state_in = State(
        leader=True,
        config=config,
        relations=[llmisvc_relation_ready],
        secrets=[hf_token_secret],
    )
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, BlockedStatus, "storage-initializer-image")


def test_hf_token_on_non_hf_uri_blocks(
    ctx, llmisvc_relation_ready, s3_relation, mock_s3_connection_info, hf_token_secret
):
    """Setting hf-token-secret with a non-hf:// model-uri blocks the charm."""
    config = {
        "model-uri": "s3://my-bucket/models/pythia-70m",
        "runtime-image": "img:latest",
        "storage-initializer-image": "si:img",
        "hf-token-secret": hf_token_secret.id,
    }
    state_in = State(
        leader=True,
        config=config,
        relations=[llmisvc_relation_ready, s3_relation],
        secrets=[hf_token_secret],
    )
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, BlockedStatus, "only supported with an hf://")


def test_hf_token_secret_missing_key_blocks(ctx, llmisvc_relation_ready):
    """A token secret without the expected 'token' key blocks the charm."""
    bad_secret = Secret(tracked_content={"wrong-key": "value"})
    config = {
        "model-uri": "hf://google/gemma-3-270m-it",
        "runtime-image": "img:latest",
        "storage-initializer-image": "si:img",
        "hf-token-secret": bad_secret.id,
    }
    state_in = State(
        leader=True,
        config=config,
        relations=[llmisvc_relation_ready],
        secrets=[bad_secret],
    )
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, BlockedStatus, "'token' key")


def test_ready_and_cr_ready_becomes_active(
    ctx, ready_state, mock_krh_apply, mock_krh_lightkube_client
):
    """Valid config + ready relation + Ready CR condition -> apply once + Active."""
    mock_krh_lightkube_client.get.side_effect = None
    mock_krh_lightkube_client.get.return_value = _ready_condition("True")

    out = ctx.run(ctx.on.config_changed(), ready_state)

    mock_krh_apply.assert_called_once()
    assert_status(out, ActiveStatus)


def test_cr_not_created_yet_waits(ctx, ready_state, mock_krh_apply):
    """Valid config + ready relation but CR not created yet (404) -> apply + Waiting."""
    out = ctx.run(ctx.on.config_changed(), ready_state)

    mock_krh_apply.assert_called_once()
    assert_status(out, WaitingStatus, "to be created")


def test_cr_ready_unknown_waits(ctx, ready_state, mock_krh_lightkube_client):
    """A Ready=Unknown condition is a recoverable, progressing state -> Waiting."""
    mock_krh_lightkube_client.get.side_effect = None
    mock_krh_lightkube_client.get.return_value = _ready_condition(
        "Unknown", message="Deployment is progressing"
    )

    out = ctx.run(ctx.on.config_changed(), ready_state)

    assert_status(out, WaitingStatus, "to become Ready")


def test_cr_ready_false_blocks(ctx, ready_state, mock_krh_lightkube_client):
    """A Ready=False condition needs user intervention -> Blocked with detail."""
    mock_krh_lightkube_client.get.side_effect = None
    mock_krh_lightkube_client.get.return_value = _ready_condition(
        "False", message="Back-off pulling image"
    )

    out = ctx.run(ctx.on.config_changed(), ready_state)

    assert_status(out, BlockedStatus, "Back-off pulling image")
    assert "Manual intervention" in out.unit_status.message


def test_context_maps_config_to_manifest(ctx, ready_state):
    """The render context maps config and identity onto the manifest fields."""
    with ctx(ctx.on.config_changed(), ready_state) as manager:
        manager.run()
        context = manager.charm._context
        model_name = manager.charm.model.name

    assert context["app_name"] == "llm-integrator"
    assert context["namespace"] == model_name
    assert context["model_uri"] == "hf://EleutherAI/pythia-70m"
    assert context["model_name"] == "pythia-70m"
    assert context["runtime_image"] == "quay.io/example/vllm-cpu:latest"


def test_prefill_decode_enabled_by_default(ctx, ready_state):
    """enable-prefill-decode defaults to True in the render context."""
    with ctx(ctx.on.config_changed(), ready_state) as manager:
        manager.run()
        assert manager.charm._context["enable_prefill_decode"] is True


def test_prefill_decode_can_be_disabled(ctx, valid_config, llmisvc_relation_ready):
    """Setting enable-prefill-decode=false flips the render context flag."""
    config = {**valid_config, "enable-prefill-decode": False}
    state_in = State(leader=True, config=config, relations=[llmisvc_relation_ready])
    with ctx(ctx.on.config_changed(), state_in) as manager:
        manager.run()
        assert manager.charm._context["enable_prefill_decode"] is False


def _render_template(**overrides):
    from jinja2 import Template

    template_path = (
        Path(__file__).resolve().parents[2] / "src/templates/llm_inference_service.yaml.j2"
    )
    context = {
        "app_name": "llm-integrator",
        "namespace": "kubeflow",
        "model_uri": "hf://EleutherAI/pythia-70m",
        "model_name": "pythia-70m",
        "runtime_image": "img:latest",
        "enable_prefill_decode": True,
        "is_s3": False,
        "use_hf_token": False,
    }
    context.update(overrides)
    return Template(template_path.read_text()).render(context)


def test_template_includes_prefill_block_when_enabled():
    """The rendered manifest includes the prefill worker in prefill/decode mode."""
    assert "prefill:" in _render_template(enable_prefill_decode=True)


def test_template_omits_prefill_block_when_disabled():
    """The rendered manifest omits the prefill worker in single/colocated mode."""
    assert "prefill:" not in _render_template(enable_prefill_decode=False)


def test_template_main_port_override_only_in_disaggregated_mode():
    """vLLM overrides to :8001 only in disaggregated mode (routing sidecar owns 8000)."""
    disaggregated = _render_template(enable_prefill_decode=True)
    assert "8001" in disaggregated
    single = _render_template(enable_prefill_decode=False)
    assert "--port" not in single
    assert "8001" not in single


def _render_s3_template(**overrides):
    s3_overrides = {
        "is_s3": True,
        "model_uri": "s3://my-bucket/models/pythia-70m",
        "storage_initializer_image": "si:img",
        "s3_secret_name": "llm-integrator-s3-creds",
        "s3_access_key": "ak",
        "s3_secret_access_key": "sk",
        "s3_region": "eu-central-1",
        "s3_endpoint": "s3.eu-central-1.amazonaws.com",
        "s3_use_https": "1",
    }
    s3_overrides.update(overrides)
    return _render_template(**s3_overrides)


def test_template_includes_storage_initializer_for_s3():
    """An s3:// render disables the built-in initializer and injects a manual one."""
    rendered = _render_s3_template()
    assert "storageInitializer:" in rendered
    assert "enabled: false" in rendered
    assert "name: storage-initializer" in rendered
    assert "kserve-pvc-source" in rendered
    assert "AWS_ACCESS_KEY_ID" in rendered
    # Both the decode and prefill workers get their own storage-initializer.
    assert rendered.count("name: storage-initializer") == 2


def test_template_s3_keeps_credentials_in_secret_only():
    """s3:// creds live in a Secret referenced via secretKeyRef, never inlined."""
    rendered = _render_s3_template(
        s3_access_key="AKIA_RAW_KEY", s3_secret_access_key="RAW_SECRET_VALUE"
    )
    # A dedicated Secret carries the raw credentials as stringData.
    assert "kind: Secret" in rendered
    assert "stringData:" in rendered
    assert "name: llm-integrator-s3-creds" in rendered
    # Workers reference the Secret rather than inlining values.
    assert "secretKeyRef" in rendered
    # The raw secret appears exactly once (in the Secret), not duplicated into
    # the decode/prefill container env.
    assert rendered.count("RAW_SECRET_VALUE") == 1
    assert rendered.count("AKIA_RAW_KEY") == 1


def test_template_omits_storage_initializer_for_hf():
    """A hf:// render relies on the built-in storage initializer (no manual one)."""
    rendered = _render_template(is_s3=False)
    assert "storageInitializer:" not in rendered
    assert "storage-initializer" not in rendered
    assert "kserve-pvc-source" not in rendered
    assert "kind: Secret" not in rendered


def _render_hf_token_template(**overrides):
    hf_overrides = {
        "use_hf_token": True,
        "model_uri": "hf://google/gemma-3-270m-it",
        "model_name": "google/gemma-3-270m-it",
        "storage_initializer_image": "si:img",
        "hf_secret_name": "llm-integrator-hf-token",
        "hf_token": "hf_RAWTOKEN",
    }
    hf_overrides.update(overrides)
    return _render_template(**hf_overrides)


def test_template_includes_storage_initializer_for_hf_token():
    """A gated hf:// render injects a manual initializer that reads HF_TOKEN."""
    rendered = _render_hf_token_template()
    assert "storageInitializer:" in rendered
    assert "enabled: false" in rendered
    assert "name: storage-initializer" in rendered
    assert "kserve-pvc-source" in rendered
    assert "HF_TOKEN" in rendered
    assert "secretKeyRef" in rendered
    assert "name: llm-integrator-hf-token" in rendered
    # Both the decode and prefill workers get their own storage-initializer.
    assert rendered.count("name: storage-initializer") == 2
    # The token flows through a Secret only; no ServiceAccount is involved.
    assert "ServiceAccount" not in rendered
    assert "serviceAccountName" not in rendered


def test_template_hf_token_kept_in_secret_only():
    """hf:// token lives in a Secret referenced via secretKeyRef, never inlined."""
    rendered = _render_hf_token_template(hf_token="hf_RAWTOKEN")
    assert "kind: Secret" in rendered
    assert "stringData:" in rendered
    assert "name: llm-integrator-hf-token" in rendered
    # The raw token appears exactly once (in the Secret), not duplicated into
    # the decode/prefill container env.
    assert rendered.count("hf_RAWTOKEN") == 1


def test_remove_deletes_resource(ctx, ready_state, mock_krh_lightkube_client):
    """The remove event deletes the LLMInferenceService and its Secret."""
    out = ctx.run(ctx.on.remove(), ready_state)

    deleted_kinds = {
        call.args[0].__name__ for call in mock_krh_lightkube_client.delete.call_args_list
    }
    assert deleted_kinds == {"LLMInferenceService", "Secret"}
    assert_status(out, MaintenanceStatus, "K8s resources removed")
