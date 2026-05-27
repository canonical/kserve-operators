# kserve-llmisvc Observability

## Overview

The `kserve-llmisvc` charm exposes two Prometheus scrape targets via the
`metrics-endpoint` relation (`prometheus_scrape` interface):

| Job | Port | Source |
| --- | --- | --- |
| `llmisvc-controller` | `8080` | The llmisvc controller's own `/metrics` endpoint (controller-runtime). |
| `llmisvc-workload-aggregated` | `15090` | A `metrics-k8s-proxy` sidecar (`ubuntu/metrics-proxy:0.1.1-22.04_stable`) that discovers and aggregates `/metrics` from all `LLMInferenceService` **workload** pods in the cluster. |

The aggregator sidecar runs in the charm pod (container `metrics-proxy`)
and is configured with:

- `POD_LABEL_SELECTOR=app.kubernetes.io/part-of=llminferenceservice`
- `PORT=15090`
- `SCRAPE_TIMEOUT=9s`

Workload pods are made discoverable by Prometheus-style annotations
injected via `LLMInferenceServiceConfig.spec.annotations` in
`src/templates/llmisvc_configs_manifests.yaml.j2`:

```yaml
spec:
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: "8000"      # vLLM metrics port
    prometheus.io/path: "/metrics"
```

Aggregated samples are exposed with `k8s_pod_name` / `k8s_namespace`
labels so individual workload pods can be distinguished in Prometheus.

## Why scheduler (EPP) metrics are not aggregated

We initially also annotated the `kserve-config-llm-scheduler`
`LLMInferenceServiceConfig` so the proxy would scrape the Endpoint Picker
(EPP) `/metrics` on port `9090`. In practice the proxy logged:

```
Failed to scrape http://<epp-pod-ip>:9090/metrics, status code: 401
```

### Root cause

The EPP `/metrics` endpoint is protected by Kubernetes RBAC and requires
a bearer token from a ServiceAccount that has been granted

```yaml
nonResourceURLs: ["/metrics"]
verbs: ["get"]
```

via a `ClusterRoleBinding`. `metrics-k8s-proxy` only performs
**unauthenticated** plain HTTP scrapes, so it cannot retrieve EPP
metrics and reports the target as `up=0`.

### Resolution

The Prometheus annotations were removed from the scheduler entry in
`llmisvc_configs_manifests.yaml.j2`. The proxy now scrapes only the
workload pods, and `up{}` no longer contains a perpetually-failing
scheduler target.

## If you need EPP/scheduler metrics later

Do **not** try to route them through the in-charm aggregator. Instead,
configure a dedicated Prometheus scrape (e.g. a `ServiceMonitor` or a
COS scrape job) that:

1. Uses a `ServiceAccount` bound to a `ClusterRole` granting
   `nonResourceURLs: ["/metrics"]` with `verbs: ["get"]`.
2. Authenticates with that SA's bearer token.
3. Targets the scheduler pods on port `9090`, path `/metrics`.

## Pitfall: pod annotations on `LLMInferenceServiceConfig`

`LLMInferenceServiceConfig.spec.template` is a `core/v1.PodSpec`
(not a `PodTemplateSpec`), so it has **no `metadata` field**. Pod-level
annotations and labels live on the sibling `annotations` / `labels`
fields of `WorkloadSpec` and `SchedulerSpec`:

```yaml
spec:
  annotations: {...}   # ← here, not under spec.template.metadata
  labels: {...}
  template:
    containers: [...]
```

Putting annotations under `spec.template.metadata` is rejected by the
API server with:

```
.spec.template.metadata: field not declared in schema
```
