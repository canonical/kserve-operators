# llm-integrator

[![Charmhub](https://charmhub.io/llm-integrator/badge.svg)](https://charmhub.io/llm-integrator)

A [Juju](https://juju.is) charm that serves one large language model on KServe. It
renders a KServe `LLMInferenceService` from a few configuration options and keeps it in
sync, so you can serve a model without writing any YAML.

- Models from Hugging Face (`hf://`, including gated models) or S3 (`s3://`).
- [vLLM](https://docs.vllm.ai/) on CPU, or on one or more NVIDIA GPUs with tensor
  parallelism.
- Optional prefill/decode split, with separate prefill and decode workers.
- A fixed number of replicas, or autoscaling with [KEDA](https://keda.sh/) on live vLLM
  metrics from Prometheus.
- Status messages that tell a model that is still downloading or loading from one that
  is failing.

Deploy one application per model. The application name is the name of the model's
`LLMInferenceService`.

## Deploy

The charm needs the KServe LLM serving stack: `kserve-llmisvc` and its dependencies.
The [autoscaling-model-serving](https://github.com/canonical/autoscaling-model-serving)
Terraform solution deploys it.

Serve a small public model on CPU:

```bash
juju deploy llm-integrator pythia --channel latest/edge --trust \
  --config model-uri=hf://EleutherAI/pythia-70m
juju integrate pythia:kserve-llmisvc kserve-llmisvc:kserve-llmisvc
```

The model is served through the gateway on
`http://<gateway-address>/<model>/pythia/v1/`, where `<model>` is the Juju model:

```bash
curl "http://<gateway-address>/<model>/pythia/v1/completions" -H 'Content-Type: application/json' \
  -d '{"model": "EleutherAI/pythia-70m", "prompt": "Kubernetes is", "max_tokens": 30}'
```

`--trust` is required: the charm creates the `LLMInferenceService` and, when
autoscaling, KEDA `ScaledObjects`.

### GPU models

```bash
juju config pythia accelerator=nvidia-gpu gpu-count=2
```

Each worker requests `gpu-count` GPUs (`nvidia.com/gpu`) and vLLM splits the model
across them. The charm checks that the cluster can fit the workers before it deploys
them.

### Gated Hugging Face models

Store the token in a Juju secret, grant it to the application, then configure it:

```bash
SECRET=$(juju add-secret hf-token token=<your-token>)
juju grant-secret hf-token pythia
juju config pythia hf-token-secret="$SECRET"
```

### S3 models

Relate the charm to an [s3-integrator](https://charmhub.io/s3-integrator) that holds the
bucket's credentials, and set an `s3://` URI:

```bash
juju config pythia model-uri=s3://my-bucket/pythia-70m
juju integrate pythia:s3-credentials s3-integrator:s3-credentials
```

### Autoscaling

Set `max-replicas` above `min-replicas`, and relate the charm to `keda-controller` and
to the Prometheus that scrapes `kserve-llmisvc`:

```bash
juju config pythia max-replicas=3
juju integrate pythia:keda keda-controller:keda
juju integrate pythia:prometheus-api prometheus:prometheus-api
```

The charm creates a KEDA `ScaledObject` per worker Deployment, scaling on
`autoscaling-metric` (by default the running plus waiting requests) to keep it at
`autoscaling-target` per replica. With `max-replicas` equal to `min-replicas` the model
runs that fixed number of replicas and needs neither KEDA nor Prometheus. A missing
autoscaling dependency never stops the model from serving: the charm keeps its current
replicas and reports what is missing.

## Configuration

The main options are below. The
[Charmhub configuration page](https://charmhub.io/llm-integrator/configurations) lists
them all, with their full descriptions.

| Option | Default | Description |
| --- | --- | --- |
| `model-uri` | | Model location, `hf://<org>/<model>` or `s3://<bucket>/<path>`. Required. |
| `model-name` | from the URI | Name the model is served as. |
| `accelerator` | `cpu` | `cpu` or `nvidia-gpu`. |
| `gpu-count` | `1` | GPUs per worker, with `nvidia-gpu`. |
| `cpu-request`, `cpu-limit`, `memory-request`, `memory-limit` | per accelerator | Worker resources. |
| `max-model-len` | `0` | Maximum context length; `0` keeps the model's own. |
| `gpu-memory-utilization` | `0.0` | Fraction of GPU memory vLLM may use; `0.0` keeps vLLM's default. |
| `vllm-extra-args` | | Extra `vllm serve` arguments. |
| `hf-token-secret` | | Juju secret with a Hugging Face token, under the `token` key. |
| `enable-prefill-decode` | `false` | Run separate prefill and decode workers. |
| `min-replicas`, `max-replicas` | `1`, `1` | Replica range of each worker. Autoscaling is on when `max-replicas` is greater. |
| `autoscaling-metric` | `num-requests` | `num-requests`, `num-requests-waiting` or `kv-cache-usage`. |
| `autoscaling-target` | `0.0` | Target per replica; `0` uses the metric's default. |
| `autoscaling-polling-interval` | `15` | Seconds between metric queries. |
| `autoscaling-scale-down-delay` | `300` | Seconds the metric must stay low before a replica is removed. |
| `runtime-image`, `storage-initializer-image` | Canonical images | Override the vLLM and model download images. See [`default-custom-images.json`](src/default-custom-images.json). |

## Integrations

| Endpoint | Interface | Description |
| --- | --- | --- |
| `kserve-llmisvc` | `kserve-llmisvc-sync` | Required. Waits for the `LLMInferenceService` controller to be ready. |
| `s3-credentials` | `s3` | Optional. Credentials for `s3://` models. |
| `keda` | `keda-sync` | Optional. KEDA, for autoscaling. |
| `prometheus-api` | `prometheus_api` | Optional. The Prometheus KEDA reads the vLLM metrics from, for autoscaling. |

## Limitations

- KV-cache is not transferred between prefill and decode workers: the decode worker
  recomputes the prompt.
- Autoscaling does not support Prometheus served over TLS, or application names longer
  than 39 characters (31 with prefill/decode).
- New replicas download the model from scratch, which can take minutes for large models.

## Contributing

See the repository's [README](../../README.md) and
[CONTRIBUTING.md](../../CONTRIBUTING.md) for how to build and test the charm.

## License

The charm is distributed under the Apache License 2.0; see [LICENSE](LICENSE).
