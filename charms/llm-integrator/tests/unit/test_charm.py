# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the llm-integrator charm reconcile and cleanup behaviour."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jinja2 import Template
from lightkube.resources.core_v1 import Node
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from ops.testing import Secret, State

from config import DEFAULT_IMAGES

from .helpers import assert_status, container_status, make_node, make_pod

TEMPLATE = Path(__file__).resolve().parents[2] / "src/templates/llm_inference_service.yaml.j2"
MODEL_VOLUME = {"name": "kserve-pvc-source", "emptyDir": {}}


def _state(relation, config=None) -> State:
    """A leader State with a public hf:// model plus the given config overrides."""
    return State(
        leader=True,
        config={"model-uri": "hf://EleutherAI/pythia-70m", **(config or {})},
        relations=[relation],
    )


def _render(ctx, state: State) -> str:
    """Render the manifest template from the charm's own render context."""
    with ctx(ctx.on.config_changed(), state) as manager:
        manager.run()
        context = manager.charm._context
    return Template(TEMPLATE.read_text()).render(context)


def _llmisvc_spec(rendered: str) -> dict:
    """Return the spec of the rendered LLMInferenceService."""
    docs = [doc for doc in yaml.safe_load_all(rendered) if doc]
    return next(doc for doc in docs if doc["kind"] == "LLMInferenceService")["spec"]


def _workers(spec: dict) -> list:
    """Return the decode pod template and, when present, the prefill one."""
    if "prefill" in spec:
        return [spec["template"], spec["prefill"]["template"]]
    return [spec["template"]]


def _main(pod_template: dict) -> dict:
    return next(c for c in pod_template["containers"] if c["name"] == "main")


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
        ({"model-uri": "gs://bucket/model"}, "must start with"),
    ],
)
def test_invalid_config_blocks(ctx, llmisvc_relation_ready, config, expected_msg):
    """Invalid or incomplete configuration blocks the charm with a helpful message."""
    state_in = State(leader=True, config=config, relations=[llmisvc_relation_ready])
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, BlockedStatus, expected_msg)


@pytest.mark.parametrize(
    "config, expected_msg",
    [
        ({"accelerator": "tpu"}, "accelerator must be one of: cpu, nvidia-gpu"),
        ({"accelerator": "nvidia-gpu", "gpu-count": 0}, "gpu-count must be >= 1"),
        ({"max-model-len": -1}, "max-model-len must be >= 0"),
        (
            {"accelerator": "nvidia-gpu", "gpu-memory-utilization": 1.5},
            "gpu-memory-utilization must be in (0, 1], or 0 for vLLM's default",
        ),
        ({"memory-request": "4GB"}, "Invalid memory-request: 4GB"),
        ({"cpu-limit": "0"}, "cpu-limit must be greater than 0"),
        ({"memory-request": "32Gi"}, "memory-request (32Gi) exceeds memory-limit (8Gi)"),
        ({"vllm-extra-args": "--dtype 'bfloat16"}, "Invalid vllm-extra-args"),
        ({"vllm-extra-args": "--port 9000"}, "must not set --port"),
        (
            {"vllm-extra-args": "--max_model_len=128"},
            "use the charm's max-model-len config option",
        ),
        ({"vllm-extra-args": "-tp 2"}, "use the charm's gpu-count config option"),
        ({"vllm-extra-args": "-pp 2"}, "use the charm's gpu-count config option"),
        ({"vllm-extra-args": "--data_parallel_size=2"}, "must not set --data-parallel-size"),
        ({"vllm-extra-args": "--host 127.0.0.1"}, "must not set --host"),
        ({"vllm-extra-args": "--model other"}, "use the charm's model-uri config option"),
        ({"vllm-extra-args": "--kv-transfer-config {}"}, "KV-cache transfer is not supported"),
    ],
)
def test_invalid_workload_config_blocks(
    ctx, llmisvc_relation_ready, mock_krh_apply, config, expected_msg
):
    """Invalid accelerator, resource or vLLM options block before anything is applied."""
    out = ctx.run(ctx.on.config_changed(), _state(llmisvc_relation_ready, config))
    assert_status(out, BlockedStatus, expected_msg)
    mock_krh_apply.assert_not_called()


@pytest.mark.parametrize("config", [{"gpu-count": 0}, {"gpu-memory-utilization": 1.5}])
def test_gpu_only_options_not_validated_on_cpu(
    ctx, llmisvc_relation_ready, mock_krh_apply, config
):
    """GPU-only options are ignored on CPU, so even invalid values do not block."""
    ctx.run(ctx.on.config_changed(), _state(llmisvc_relation_ready, config))
    mock_krh_apply.assert_called_once()


def test_gpu_workload_without_gpus_in_cluster_blocks(ctx, llmisvc_relation_ready, mock_krh_apply):
    """A GPU workload on a cluster without GPUs blocks without applying anything."""
    state_in = _state(llmisvc_relation_ready, {"accelerator": "nvidia-gpu"})
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, BlockedStatus, "No node offers 1 nvidia.com/gpu")
    mock_krh_apply.assert_not_called()


def test_gpu_workload_with_gpus_in_cluster_applies(
    ctx, llmisvc_relation_ready, gpu_cluster, mock_krh_apply
):
    """A GPU workload is applied when a node offers enough GPUs."""
    state_in = _state(llmisvc_relation_ready, {"accelerator": "nvidia-gpu"})
    out = ctx.run(ctx.on.config_changed(), state_in)
    mock_krh_apply.assert_called_once()
    assert_status(out, WaitingStatus, "to be created")


def test_resources_larger_than_any_node_block(ctx, llmisvc_relation_ready, mock_krh_apply):
    """Requests no node can satisfy block without applying anything."""
    config = {"memory-request": "10Ti", "memory-limit": "10Ti"}
    out = ctx.run(ctx.on.config_changed(), _state(llmisvc_relation_ready, config))
    assert_status(out, BlockedStatus, "No node can fit a worker")
    mock_krh_apply.assert_not_called()


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


def test_storage_initializer_image_defaults_to_shipped_image(ctx, ready_state):
    """An empty storage-initializer-image falls back to the image the charm ships."""
    with ctx(ctx.on.config_changed(), ready_state) as manager:
        manager.run()
        image = manager.charm._context["storage_initializer_image"]
    assert image == DEFAULT_IMAGES["storage_initializer"]


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


@pytest.mark.parametrize(
    "pod, expected_status, expected_msg",
    [
        (
            make_pod(init_containers=[container_status("storage-initializer", running=True)]),
            WaitingStatus,
            "is starting: downloading the model",
        ),
        (
            make_pod(
                init_containers=[
                    container_status(
                        "storage-initializer",
                        waiting_reason="CrashLoopBackOff",
                        restarts=2,
                        last_exit_code=137,
                        last_reason="OOMKilled",
                    )
                ]
            ),
            BlockedStatus,
            "storage-initializer keeps failing (OOMKilled, exit code 137)",
        ),
    ],
)
def test_unavailable_workload_status_comes_from_pods(
    ctx, ready_state, mock_krh_lightkube_client, pod, expected_status, expected_msg
):
    """An unavailable workload is Waiting while it starts and Blocked when a pod fails."""
    mock_krh_lightkube_client.get.side_effect = None
    mock_krh_lightkube_client.get.return_value = _ready_condition(
        "False",
        reason="MinimumReplicasUnavailable",
        message="Deployment does not have minimum availability.",
    )
    mock_krh_lightkube_client.list.side_effect = lambda resource, **_: (
        [make_node()] if resource is Node else [pod]
    )

    out = ctx.run(ctx.on.config_changed(), ready_state)

    assert_status(out, expected_status, expected_msg)


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


def test_prefill_decode_disabled_by_default(ctx, ready_state):
    """enable-prefill-decode defaults to False in the render context."""
    with ctx(ctx.on.config_changed(), ready_state) as manager:
        manager.run()
        assert manager.charm._context["enable_prefill_decode"] is False


def test_prefill_decode_can_be_enabled(ctx, valid_config, llmisvc_relation_ready):
    """Setting enable-prefill-decode=true flips the render context flag."""
    config = {**valid_config, "enable-prefill-decode": True}
    state_in = State(leader=True, config=config, relations=[llmisvc_relation_ready])
    with ctx(ctx.on.config_changed(), state_in) as manager:
        manager.run()
        assert manager.charm._context["enable_prefill_decode"] is True


def test_template_includes_prefill_block_only_when_enabled(ctx, llmisvc_relation_ready):
    """The prefill worker is rendered only in prefill/decode mode."""
    enabled = _render(ctx, _state(llmisvc_relation_ready, {"enable-prefill-decode": True}))
    disabled = _render(ctx, _state(llmisvc_relation_ready))
    assert "prefill" in _llmisvc_spec(enabled)
    assert "prefill" not in _llmisvc_spec(disabled)


def test_template_main_port_override_only_in_disaggregated_mode(ctx, llmisvc_relation_ready):
    """vLLM moves to :8001 only in disaggregated mode (routing sidecar owns 8000)."""
    pd = _render(ctx, _state(llmisvc_relation_ready, {"enable-prefill-decode": True}))
    single = _render(ctx, _state(llmisvc_relation_ready))
    assert _main(_llmisvc_spec(pd)["template"])["args"] == ["--enforce-eager", "--port", "8001"]
    assert _main(_llmisvc_spec(single)["template"])["args"] == ["--enforce-eager"]


@pytest.mark.parametrize(
    "config, replicas",
    [
        pytest.param({}, 1, id="default"),
        pytest.param({"min-replicas": 3, "max-replicas": 3}, 3, id="fixed"),
        pytest.param({"max-replicas": 3}, None, id="autoscaling"),
    ],
)
def test_template_replicas(ctx, llmisvc_relation_ready, config, replicas):
    """Fixed replicas are rendered for both workers; while autoscaling KEDA owns them."""
    config = {**config, "enable-prefill-decode": True}
    spec = _llmisvc_spec(_render(ctx, _state(llmisvc_relation_ready, config)))
    assert spec.get("replicas") == replicas
    assert spec["prefill"].get("replicas") == replicas


def test_template_cpu_worker_defaults(ctx, llmisvc_relation_ready):
    """CPU workers use the CPU image, CPU KV cache and CPU-only resource defaults."""
    spec = _llmisvc_spec(
        _render(ctx, _state(llmisvc_relation_ready, {"enable-prefill-decode": True}))
    )
    for worker in _workers(spec):
        main = _main(worker)
        assert main["image"] == DEFAULT_IMAGES["vllm"]
        assert {"name": "VLLM_CPU_KVCACHE_SPACE", "value": "1"} in main["env"]
        assert main["resources"] == {
            "requests": {"cpu": "500m", "memory": "4Gi"},
            "limits": {"cpu": "2", "memory": "8Gi"},
        }
        assert worker["volumes"] == [MODEL_VOLUME]


def test_template_gpu_worker_defaults(ctx, llmisvc_relation_ready, gpu_cluster):
    """GPU workers use the GPU image, request a GPU and drop the CPU-only settings."""
    config = {"accelerator": "nvidia-gpu", "enable-prefill-decode": True}
    decode, prefill = _workers(_llmisvc_spec(_render(ctx, _state(llmisvc_relation_ready, config))))
    for worker in (decode, prefill):
        main = _main(worker)
        assert main["image"] == DEFAULT_IMAGES["vllm_gpu"]
        assert all(env["name"] != "VLLM_CPU_KVCACHE_SPACE" for env in main["env"])
        assert main["resources"] == {
            "requests": {"cpu": "2", "memory": "8Gi", "nvidia.com/gpu": "1"},
            "limits": {"cpu": "4", "memory": "16Gi", "nvidia.com/gpu": "1"},
        }
        assert worker["volumes"] == [MODEL_VOLUME]
    assert _main(decode)["args"] == ["--port", "8001"]
    assert _main(prefill)["args"] == ["--enable-chunked-prefill"]


def test_template_multi_gpu_sets_tensor_parallelism_and_shm(
    ctx, llmisvc_relation_ready, gpu_cluster
):
    """gpu-count > 1 shards the model across GPUs and enlarges /dev/shm."""
    config = {"accelerator": "nvidia-gpu", "gpu-count": 4}
    worker = _llmisvc_spec(_render(ctx, _state(llmisvc_relation_ready, config)))["template"]
    main = _main(worker)
    assert main["args"] == ["--tensor-parallel-size", "4"]
    assert main["resources"]["limits"]["nvidia.com/gpu"] == "4"
    assert worker["volumes"] == [
        MODEL_VOLUME,
        {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "8Gi"}},
    ]


def test_template_vllm_options_render_as_args(ctx, llmisvc_relation_ready, gpu_cluster):
    """vLLM tuning options and extra args reach both workers verbatim."""
    config = {
        "accelerator": "nvidia-gpu",
        "enable-prefill-decode": True,
        "max-model-len": 8192,
        "gpu-memory-utilization": 0.4,
        "vllm-extra-args": """--dtype bfloat16 --override-generation-config '{"temperature": 0.5}'""",
    }
    shared = [
        "--max-model-len",
        "8192",
        "--gpu-memory-utilization",
        "0.4",
        "--dtype",
        "bfloat16",
        "--override-generation-config",
        '{"temperature": 0.5}',
    ]
    decode, prefill = _workers(_llmisvc_spec(_render(ctx, _state(llmisvc_relation_ready, config))))
    assert _main(decode)["args"] == ["--port", "8001", *shared]
    assert _main(prefill)["args"] == ["--enable-chunked-prefill", *shared]


def test_template_ignores_gpu_options_on_cpu(ctx, llmisvc_relation_ready):
    """gpu-count and gpu-memory-utilization have no effect on CPU workers."""
    config = {"gpu-count": 4, "gpu-memory-utilization": 0.4}
    worker = _llmisvc_spec(_render(ctx, _state(llmisvc_relation_ready, config)))["template"]
    main = _main(worker)
    assert main["args"] == ["--enforce-eager"]
    assert "nvidia.com/gpu" not in main["resources"]["limits"]
    assert worker["volumes"] == [MODEL_VOLUME]


def test_template_image_and_resource_overrides(ctx, llmisvc_relation_ready):
    """Explicit runtime-image and resource options replace the accelerator defaults."""
    config = {
        "runtime-image": "custom:img",
        "cpu-request": "1",
        "cpu-limit": "3",
        "memory-request": "6Gi",
        "memory-limit": "12Gi",
    }
    main = _main(_llmisvc_spec(_render(ctx, _state(llmisvc_relation_ready, config)))["template"])
    assert main["image"] == "custom:img"
    assert main["resources"] == {
        "requests": {"cpu": "1", "memory": "6Gi"},
        "limits": {"cpu": "3", "memory": "12Gi"},
    }


@pytest.fixture
def s3_state(llmisvc_relation_ready, s3_config, s3_relation, mock_s3_connection_info):
    """A ready state for an s3:// model in prefill/decode mode with raw test credentials."""
    mock_s3_connection_info.return_value = {
        **mock_s3_connection_info.return_value,
        "access-key": "AKIA_RAW_KEY",
        "secret-key": "RAW_SECRET_VALUE",
    }
    return State(
        leader=True,
        config={**s3_config, "enable-prefill-decode": True},
        relations=[llmisvc_relation_ready, s3_relation],
    )


@pytest.fixture
def hf_token_state(llmisvc_relation_ready, hf_token_secret):
    """A ready state for a gated hf:// model in prefill/decode mode."""
    config = {
        "model-uri": "hf://google/gemma-3-270m-it",
        "hf-token-secret": hf_token_secret.id,
        "enable-prefill-decode": True,
    }
    return State(
        leader=True,
        config=config,
        relations=[llmisvc_relation_ready],
        secrets=[hf_token_secret],
    )


def test_template_includes_storage_initializer_for_s3(ctx, s3_state):
    """An s3:// render disables the built-in initializer and injects a manual one."""
    rendered = _render(ctx, s3_state)
    spec = _llmisvc_spec(rendered)
    assert spec["storageInitializer"] == {"enabled": False}
    for worker in _workers(spec):
        assert [c["name"] for c in worker["initContainers"]] == ["storage-initializer"]
        assert worker["volumes"] == [MODEL_VOLUME]
    assert "AWS_ACCESS_KEY_ID" in rendered


def test_template_s3_keeps_credentials_in_secret_only(ctx, s3_state):
    """s3:// creds live in a Secret referenced via secretKeyRef, never inlined."""
    rendered = _render(ctx, s3_state)
    assert "kind: Secret" in rendered
    assert "stringData:" in rendered
    assert "name: llm-integrator-s3-creds" in rendered
    assert "secretKeyRef" in rendered
    # Each raw value appears once (in the Secret), not in the worker env.
    assert rendered.count("RAW_SECRET_VALUE") == 1
    assert rendered.count("AKIA_RAW_KEY") == 1


def test_template_includes_storage_initializer_for_public_hf(ctx, llmisvc_relation_ready):
    """A public hf:// render uses the charm's own initializer, without a token."""
    rendered = _render(ctx, _state(llmisvc_relation_ready))
    spec = _llmisvc_spec(rendered)
    assert spec["storageInitializer"] == {"enabled": False}
    initializer = spec["template"]["initContainers"][0]
    assert initializer["name"] == "storage-initializer"
    assert initializer["args"] == ["hf://EleutherAI/pythia-70m", "/mnt/models"]
    assert [env["name"] for env in initializer["env"]] == [
        "HF_HUB_ENABLE_HF_TRANSFER",
        "HF_HUB_DOWNLOAD_TIMEOUT",
    ]
    assert "resources" not in initializer
    assert "kind: Secret" not in rendered


def test_template_includes_storage_initializer_for_hf_token(ctx, hf_token_state):
    """A gated hf:// render injects a manual initializer that reads HF_TOKEN."""
    rendered = _render(ctx, hf_token_state)
    spec = _llmisvc_spec(rendered)
    assert spec["storageInitializer"] == {"enabled": False}
    for worker in _workers(spec):
        initializer = worker["initContainers"][0]
        assert initializer["name"] == "storage-initializer"
        assert {
            "name": "HF_TOKEN",
            "valueFrom": {"secretKeyRef": {"name": "llm-integrator-hf-token", "key": "HF_TOKEN"}},
        } in initializer["env"]
    # The token flows through a Secret only; no ServiceAccount is involved.
    assert "ServiceAccount" not in rendered
    assert "serviceAccountName" not in rendered


def test_template_hf_token_kept_in_secret_only(ctx, hf_token_state):
    """hf:// token lives in a Secret referenced via secretKeyRef, never inlined."""
    rendered = _render(ctx, hf_token_state)
    assert "kind: Secret" in rendered
    assert "name: llm-integrator-hf-token" in rendered
    assert rendered.count("hf_secrettoken") == 1


# Quotes, a YAML comment, a document separator and a backslash.
INJECTION = 'evil" #: x\n---\nkind: Namespace\\'


def test_template_escapes_config_values(ctx, llmisvc_relation_ready):
    """Config values cannot break the YAML or inject extra documents."""
    config = {"model-name": INJECTION, "runtime-image": INJECTION}
    rendered = _render(ctx, _state(llmisvc_relation_ready, config))
    docs = [doc for doc in yaml.safe_load_all(rendered) if doc]
    assert [doc["kind"] for doc in docs] == ["LLMInferenceService"]
    spec = docs[0]["spec"]
    assert spec["model"]["name"] == INJECTION
    assert _main(spec["template"])["image"] == INJECTION


def test_template_escapes_s3_credentials(ctx, s3_state, mock_s3_connection_info):
    """Relation-supplied credentials are rendered verbatim, whatever characters they hold."""
    mock_s3_connection_info.return_value = {
        **mock_s3_connection_info.return_value,
        "secret-key": INJECTION,
    }
    docs = [doc for doc in yaml.safe_load_all(_render(ctx, s3_state)) if doc]
    assert [doc["kind"] for doc in docs] == ["Secret", "LLMInferenceService"]
    assert docs[0]["stringData"]["AWS_SECRET_ACCESS_KEY"] == INJECTION


def test_remove_deletes_resource(ctx, ready_state, mock_krh_lightkube_client):
    """The remove event deletes the ScaledObjects first, then the CR and the Secrets."""
    out = ctx.run(ctx.on.remove(), ready_state)

    deleted = [
        (call.args[0].__name__, call.kwargs["name"])
        for call in mock_krh_lightkube_client.delete.call_args_list
    ]
    assert deleted == [
        ("ScaledObject", "llm-integrator-kserve"),
        ("ScaledObject", "llm-integrator-kserve-prefill"),
        ("LLMInferenceService", "llm-integrator"),
        ("Secret", "llm-integrator-s3-creds"),
        ("Secret", "llm-integrator-hf-token"),
    ]
    assert_status(out, MaintenanceStatus, "K8s resources removed")
