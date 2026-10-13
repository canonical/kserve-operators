# kserve-llmisvc

[![Charmhub](https://charmhub.io/kserve-llmisvc/badge.svg)](https://charmhub.io/kserve-llmisvc)

A [Juju](https://juju.is) charm for KServe's `LLMInferenceService` controller, from
[KServe](https://kserve.github.io/website/) 0.17.0. It serves large language models with
[vLLM](https://docs.vllm.ai/) and the [llm-d](https://llm-d.ai/) inference scheduler.

For each `LLMInferenceService`, the controller creates the vLLM worker Deployments, a
router-scheduler that picks a worker for each request, and the gateway route that
exposes the model. The charm also:

- installs the `LLMInferenceServiceConfig` presets the controller builds workloads from;
- collects the metrics of every vLLM worker in the cluster and exposes them for
  Prometheus;
- ships Prometheus alert rules and Grafana dashboards for the controller and the vLLM
  workloads;
- forwards its logs to Loki, and passes the Loki address to the vLLM workers so they
  push theirs.

Models are usually created with the [`llm-integrator`](../llm-integrator) charm, but any
`LLMInferenceService` manifest works.

## Deploy

`kserve-llmisvc` needs `kserve-controller` (in `standard` mode, related to a gateway)
and `lws-controller`:

```bash
juju deploy kserve-llmisvc --channel latest/edge --trust
juju deploy lws-controller --channel latest/edge --trust

juju integrate kserve-controller:kserve-controller kserve-llmisvc:kserve-controller
juju integrate lws-controller:lws-controller kserve-llmisvc:lws-controller
```

`--trust` is required: the charm installs cluster-scoped resources such as CRDs and
webhooks.

The charm waits until both controllers report that they are ready, then publishes its
own readiness on the `kserve-llmisvc` endpoint. The
[autoscaling-model-serving](https://github.com/canonical/autoscaling-model-serving)
Terraform solution deploys and wires the whole stack.

## Configuration

| Option | Description |
| --- | --- |
| `custom_images` | Overrides, as YAML or JSON, of the router-scheduler (`llm_scheduler`), routing sidecar (`llm_routing_sidecar`) and workload (`llm_workload`) images. See [`default-custom-images.json`](src/default-custom-images.json). |

## Integrations

| Endpoint | Interface | Description |
| --- | --- | --- |
| `kserve-controller` | `kserve-controller-sync` | Waits for `kserve-controller` to be ready. |
| `lws-controller` | `lws-controller-sync` | Waits for `lws-controller`, which multi-node workloads need. |
| `kserve-llmisvc` | `kserve-llmisvc-sync` | Tells model charms such as `llm-integrator` that they can create `LLMInferenceServices`. |
| `metrics-endpoint` | `prometheus_scrape` | Controller and vLLM workload metrics, with alert rules. |
| `grafana-dashboard` | `grafana_dashboard` | Controller and vLLM workload dashboards. |
| `logging` | `loki_push_api` | Optional. Forwards the charm's logs to Loki, and the vLLM workers' logs too. |

## Observability

| Signal | Where |
| --- | --- |
| Alert rules | [`src/prometheus_alert_rules`](src/prometheus_alert_rules) |
| Dashboards | [`src/grafana_dashboards`](src/grafana_dashboards) |
| Metrics | Two scrape jobs: the controller, and every vLLM worker aggregated by the `metrics-proxy` container. vLLM metrics carry the `k8s_namespace` and `k8s_pod_name` labels. |

## Contributing

See the repository's [README](../../README.md) and
[CONTRIBUTING.md](../../CONTRIBUTING.md) for how to build and test the charm, and how to
upgrade the upstream KServe version.

## License

The charm is distributed under the Apache License 2.0; see [LICENSE](LICENSE).
