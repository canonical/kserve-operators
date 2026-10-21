# kserve-controller

[![Charmhub](https://charmhub.io/kserve-controller/badge.svg)](https://charmhub.io/kserve-controller)

A [Juju](https://juju.is) charm for the [KServe](https://kserve.github.io/website/)
controller, the control plane of KServe. It installs KServe's custom resources,
webhooks and configuration, and runs the controller that turns `InferenceService`
resources into running model servers.

The charm runs KServe in one of two modes:

- **`serverless`** (default): serverless `InferenceService` workloads with Knative
  Serving, behind an Istio sidecar gateway.
- **`rawdeployment`**: `InferenceService` workloads as plain Kubernetes Deployments,
  without Knative.

It is used as part of [Charmed Kubeflow](https://canonical.com/mlops/kubeflow).

## Deploy

```bash
juju deploy kserve-controller --channel 0.14/stable --trust
```

`--trust` is required: the charm installs cluster-scoped resources:

- the KServe CRDs: `InferenceService`, `InferenceGraph`, `TrainedModel`,
  `ServingRuntime`, `ClusterServingRuntime` and `ClusterStorageContainer`;
- the default `ClusterServingRuntimes` and `ClusterStorageContainer`;
- KServe's `MutatingWebhookConfiguration` and `ValidatingWebhookConfigurations`;
- the controller's `ClusterRoles` and `ClusterRoleBindings`.

In `serverless` mode (the default), relate the charm to the Istio sidecar gateway and
Knative Serving. To use `rawdeployment` mode instead, deploy with
`--config deployment-mode=rawdeployment`; no gateway relations are needed.

```bash
juju integrate istio-pilot:gateway-info kserve-controller:ingress-gateway
juju integrate knative-serving:local-gateway kserve-controller:local-gateway
```

## Configuration

| Option | Default | Description |
| --- | --- | --- |
| `deployment-mode` | `serverless` | `serverless` for serverless serving with Knative, or `rawdeployment` for plain Deployments (RawDeployment). |
| `domain-name` | `example.com` | Domain used in the model URLs. In `serverless` mode it must match Knative Serving's domain. |
| `custom_images` | defaults | Overrides of the images KServe workloads use, as YAML or JSON. See [`default-custom-images.json`](src/default-custom-images.json). |
| `http-proxy`, `https-proxy`, `no-proxy` | `""` | Proxy settings for the storage initializer that downloads models. |

For example, to override two images:

```bash
juju config kserve-controller custom_images='{"configmap__agent": "custom:1.0", "serving_runtimes__lgbserver": "custom:2.1"}'
```

## Integrations

| Endpoint | Interface | Description |
| --- | --- | --- |
| `ingress-gateway` | `istio-gateway-info` | The Istio gateway, in `serverless` mode. |
| `local-gateway` | `serving-local-gateway` | Knative Serving's local gateway, in `serverless` mode. |
| `object-storage` | `object-storage` | Optional. Credentials for models stored in S3-compatible storage. |
| `secrets`, `service-accounts` | `kubernetes_manifest` | Share those credentials with user namespaces, through Charmed Kubeflow's resource dispatcher. |
| `metrics-endpoint` | `prometheus_scrape` | Controller metrics, for example for COS. |
| `logging` | `loki_push_api` | Optional. Forwards logs to Loki. |

## Contributing

See the repository's [README](../../README.md) and
[CONTRIBUTING.md](../../CONTRIBUTING.md) for how to build and test the charm, and
[CONTRIBUTING.md](CONTRIBUTING.md) in this directory for charm-specific notes.

## License

The charm is distributed under the Apache License 2.0; see [LICENSE](LICENSE).
