# KServe operators

[![On Pull Request](https://github.com/canonical/kserve-operators/actions/workflows/on_pull_request.yaml/badge.svg)](https://github.com/canonical/kserve-operators/actions/workflows/on_pull_request.yaml)
[![Release](https://github.com/canonical/kserve-operators/actions/workflows/release.yaml/badge.svg)](https://github.com/canonical/kserve-operators/actions/workflows/release.yaml)

Juju charms that deploy [KServe](https://kserve.github.io/website/) on Kubernetes, for
`InferenceService` workloads, as part of
[Charmed Kubeflow](https://canonical.com/mlops/kubeflow).

## Charms

| Charm | Charmhub | Description |
| --- | --- | --- |
| [`kserve-controller`](charms/kserve-controller) | [![kserve-controller](https://charmhub.io/kserve-controller/badge.svg)](https://charmhub.io/kserve-controller) | KServe control plane, in serverless (Knative) or RawDeployment mode. |

## Model serving

`kserve-controller` runs KServe in one of two modes, set by its `deployment-mode`
option:

- `serverless`, the default: serverless serving with scale to zero, behind an Istio
  sidecar gateway and Knative Serving.
- `rawdeployment`: plain Kubernetes Deployments (RawDeployment), without Knative.

It is deployed as part of [Charmed Kubeflow](https://canonical.com/mlops/kubeflow). See
the [charm README](charms/kserve-controller/README.md) for how to deploy and configure
it.

### Custom images

`kserve-controller` comes with a set of images used by KServe workloads, listed in
[`default-custom-images.json`](charms/kserve-controller/src/default-custom-images.json).
Override any of them with the `custom_images` option, in YAML or JSON. Images you do
not set keep their defaults:

```bash
juju config kserve-controller custom_images='{"configmap__agent": "custom:1.0", "serving_runtimes__lgbserver": "custom:2.1"}'
```

## Development

The repository holds the charm under [`charms`](charms) and the documentation under
[`docs`](docs). `tox` is the only tool you need; it installs the rest.

```bash
tox -e controller-lint                              # lint the charm
tox -e controller-unit                              # unit-test the charm
tox -e controller-integration -- --model kubeflow   # integration tests
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for managing dependencies and upgrading the
upstream KServe version. To build and preview the documentation locally, run
`make run` in the `docs` directory.

## Contributing

Canonical welcomes contributions. Please read the
[contributor licence agreement](https://canonical.com/legal/contributors) and
[`CONTRIBUTING.md`](CONTRIBUTING.md) before opening a pull request.

Report bugs and request features in
[GitHub issues](https://github.com/canonical/kserve-operators/issues).

## Security

Please do not report security issues in GitHub issues. Report them through
[Launchpad](https://wiki.ubuntu.com/DebuggingSecurity) instead.

## License

The charms are distributed under the Apache License 2.0; see the `LICENSE` file in each
charm's directory.
