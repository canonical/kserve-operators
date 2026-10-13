# kserve-controller

[![Charmhub](https://charmhub.io/kserve-controller/badge.svg)](https://charmhub.io/kserve-controller)

A [Juju](https://juju.is) charm for the [KServe](https://kserve.github.io/website/)
controller, the control plane of KServe 0.17.0. It installs KServe's custom resources,
webhooks and configuration, and runs the controller that turns `InferenceService`
resources into running model servers.

The charm is used in two setups:

- **Charmed llm-d** (LLM serving), with `kserve-llmisvc` and an Envoy gateway, in `standard` mode. See
  the [Charmed llm-d documentation](https://github.com/canonical/kserve-operators/tree/main/docs).
- **Classic model serving**, as part of
  [Charmed Kubeflow](https://canonical.com/mlops/kubeflow), usually in `knative` mode.

## Deploy

```bash
juju deploy kserve-controller --channel latest/edge --trust \
  --config deployment-mode=standard
```

`--trust` is required: the charm installs cluster-scoped resources such as CRDs and
webhooks.

The charm stays `blocked` until it is related to a gateway:

- For LLM serving, relate it to the Envoy gateway:

  ```bash
  juju integrate kserve-controller:gateway-metadata envoy-ingress-k8s:gateway-metadata
  ```

- In `knative` mode, relate it to Istio and Knative Serving:

  ```bash
  juju integrate istio-pilot:gateway-info kserve-controller:ingress-gateway
  juju integrate knative-serving:local-gateway kserve-controller:local-gateway
  ```

The [autoscaling-model-serving](https://github.com/canonical/autoscaling-model-serving)
Terraform solution deploys and wires both setups for you.

## Configuration

| Option | Default | Description |
| --- | --- | --- |
| `deployment-mode` | `knative` | `knative` for serverless serving with Knative, or `standard` for plain Deployments (RawDeployment). |
| `domain-name` | `example.com` | Domain used in the model URLs. In `knative` mode it must match Knative Serving's domain. |
| `custom_images` | defaults | Overrides of the images KServe workloads use, as YAML or JSON. See [`default-custom-images.json`](src/default-custom-images.json). |
| `http-proxy`, `https-proxy`, `no-proxy` | `""` | Proxy settings for the storage initializer that downloads models. |

For example, to override two images:

```bash
juju config kserve-controller custom_images='{"configmap__agent": "custom:1.0", "serving_runtimes__lgbserver": "custom:2.1"}'
```

## Integrations

| Endpoint | Interface | Description |
| --- | --- | --- |
| `gateway-metadata` | `gateway_metadata` | The Gateway API gateway to attach models to, from `envoy-ingress-k8s` or `istio-ingress-k8s`. |
| `ingress-gateway` | `istio-gateway-info` | The Istio gateway, in `knative` mode. |
| `local-gateway` | `serving-local-gateway` | Knative Serving's local gateway, in `knative` mode. |
| `service-mesh` | `service_mesh` | Enrols the charm in an Istio ambient mesh. |
| `kserve-controller` | `kserve-controller-sync` | Tells `kserve-llmisvc` that KServe is ready. |
| `object-storage`, `s3-credentials` | `object-storage`, `s3` | Optional. Credentials for models stored in S3-compatible storage. |
| `secrets`, `service-accounts` | `kubernetes_manifest` | Share those credentials with user namespaces, through Charmed Kubeflow's resource dispatcher. |
| `metrics-endpoint` | `prometheus_scrape` | Controller metrics, for example for COS. |
| `logging` | `loki_push_api` | Optional. Forwards logs to Loki. |

## Contributing

See the repository's [README](../../README.md) and
[CONTRIBUTING.md](../../CONTRIBUTING.md) for how to build and test the charm, and
[CONTRIBUTING.md](CONTRIBUTING.md) in this directory for charm-specific notes.

## License

The charm is distributed under the Apache License 2.0; see [LICENSE](LICENSE).
