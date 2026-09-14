# KEDA charm

A [Juju](https://juju.is) charm that packages [KEDA](https://keda.sh)
(Kubernetes Event-driven Autoscaling) as a single sidecar application: the
operator, the external-metrics API server and the admission webhooks run as
three workload containers in one pod. The charm installs the KEDA CRDs and RBAC,
registers the `external.metrics.k8s.io` APIService, and manages the serving
certificates itself.

It is workload-agnostic: it does not create `ScaledObject`s. Users (or other
charms) create their own `ScaledObject`/`ScaledJob`/`TriggerAuthentication`
resources once KEDA is deployed.

## Deploy

```bash
juju deploy keda --trust
```

`--trust` is required: the charm needs cluster-scoped permissions to install the
CRDs, RBAC, the APIService and the webhook configuration.

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `watch-namespace` | `""` | Namespace KEDA restricts its watch to. Empty means cluster-wide. |
| `log-level` | `info` | Operator log level (`debug`, `info`, `error`, or an integer > 0). |

```bash
juju config keda watch-namespace=my-namespace
```

## Relations

| Endpoint | Interface | Role | Description |
|----------|-----------|------|-------------|
| `keda` | `keda-sync` | provides | Readiness contract: publishes `ready=true` once KEDA is reconciled, so dependents can gate on it. |

## How it works

- **Single pod, three containers.** The metrics-apiserver proxies to the
  operator's gRPC metrics service over `127.0.0.1:9666` (same pod), so no
  operator Service is needed.
- **Certificates.** The charm generates a self-signed CA and serving certificate
  covering both KEDA Services and `127.0.0.1`, pushes them into `/certs` in every
  container over Pebble, and sets the `caBundle` on the APIService and webhook
  configuration. KEDA's built-in cert rotation is disabled (a Juju sidecar cannot
  mount the rotation-managed Secret into the workload containers).
- **External metrics.** The `external.metrics.k8s.io` APIService points at the
  charm-managed metrics-apiserver Service; HorizontalPodAutoscalers created by
  KEDA read scaling metrics through the Kubernetes aggregation layer.

## Cleanup

Removing the application deletes the CRDs (cascading to any `ScaledObject`s) and
the singleton `external.metrics.k8s.io` APIService, so the aggregation layer is
left clean.

## KEDA version

This charm ships KEDA **2.17.3**, matching the version vendored by KServe 0.17.0.
