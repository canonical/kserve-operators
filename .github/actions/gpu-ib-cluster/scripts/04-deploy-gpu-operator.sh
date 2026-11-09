#!/usr/bin/env bash

# Installs the NVIDIA GPU Operator (which builds and loads the NVIDIA driver
# on the workers) with Helm and waits for the GPUs to be advertised (what the
# nvidia_gpu_operator unit runs).
#
# Outputs:
#   stdout: GPU nodes with product and count
#   Exit code: 0 on success, non-zero on failure

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
require_kubeconfig

values="$STATE_DIR/gpu-operator-values.yaml"
cat > "$values" <<YAML
operator:
  defaultRuntime: containerd
driver:
  enabled: true
  usePrecompiled: false
  rdma:
    enabled: false
    useHostMofed: false
mig:
  strategy: mixed
dcgmExporter:
  enabled: true
YAML

log "Installing gpu-operator $GPU_OPERATOR_VERSION"
helm upgrade --install nvidia-gpu-operator gpu-operator \
  --kubeconfig "$KUBECONFIG_PATH" \
  --repo "$NVIDIA_HELM_REPO" --version "$GPU_OPERATOR_VERSION" \
  --namespace "$GPU_OPERATOR_NAMESPACE" --create-namespace \
  -f "$values" --wait --timeout 10m

log "Waiting for ClusterPolicy to be ready (driver build included, up to 30m)"
kctl wait clusterpolicy/cluster-policy --for=jsonpath='{.status.state}'=ready --timeout=30m

[[ -n "$(kctl get nodes -l nvidia.com/gpu.present=true -o name)" ]] \
  || die "ClusterPolicy is ready but no GPU nodes were detected"

kctl wait nodes -l nvidia.com/gpu.present=true \
  --for=jsonpath='{.metadata.labels.nvidia\.com/gpu\.product}' --timeout=3m
kctl wait nodes -l nvidia.com/gpu.present=true \
  --for=jsonpath='{.metadata.labels.nvidia\.com/gpu\.count}' --timeout=3m

kctl get nodes -l nvidia.com/gpu.present=true \
  -o custom-columns='NODE:.metadata.name,PRODUCT:.metadata.labels.nvidia\.com/gpu\.product,COUNT:.metadata.labels.nvidia\.com/gpu\.count,ALLOCATABLE:.status.allocatable.nvidia\.com/gpu'
log "GPU operator ready."
