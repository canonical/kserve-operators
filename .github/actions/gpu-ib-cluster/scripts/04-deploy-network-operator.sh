#!/usr/bin/env bash

# Installs the NVIDIA Network Operator with Helm, prepares the IB kernel
# modules and PCI ACS on the workers, applies the NicClusterPolicy (RDMA shared
# device plugin) and runs an ib_send_bw test between the two workers (what the
# nvidia_network_operator and nic_cluster_policy units run).
#
# Outputs:
#   stdout: progress and ib_send_bw results
#   Exit code: 0 on success, non-zero on failure

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
require_cluster_data
require_kubeconfig

workers_json="$(worker_addresses_json)"

# NFD is provided by the GPU operator; its CRDs must exist before this chart.
log "Installing network-operator $NETWORK_OPERATOR_VERSION"
helm upgrade --install network-operator network-operator \
  --kubeconfig "$KUBECONFIG_PATH" \
  --repo "$NVIDIA_HELM_REPO" --version "$NETWORK_OPERATOR_VERSION" \
  --namespace "$NETWORK_OPERATOR_NAMESPACE" --create-namespace \
  --set nfd.enabled=false --set sriovNetworkOperator.enabled=false \
  --wait --timeout 10m

log "Preparing IB kernel modules (and PCI ACS) on the workers"
bash "$IB_SCRIPTS/prep-ib-kernel-modules.sh" "$SSH_PRIVATE_KEY" "$workers_json" \
  "$IB_IFNAMES_JSON" "$DISABLE_ACS"

log "Applying NicClusterPolicy"
plugin_config="$(jq -cn --arg r "$RDMA_RESOURCE_NAME" --argjson max "$RDMA_HCA_MAX" \
  --argjson ifs "$IB_IFNAMES_JSON" \
  '{configList: [{resourceName: $r, rdmaHcaMax: $max, selectors: {ifNames: ($ifs | unique)}}]}')"
jq -n --arg cfg "$plugin_config" '{
  apiVersion: "mellanox.com/v1alpha1",
  kind: "NicClusterPolicy",
  metadata: {name: "nic-cluster-policy"},
  spec: {rdmaSharedDevicePlugin: {
    repository: "docker.io/mellanox",
    image: "k8s-rdma-shared-dev-plugin",
    version: "v1.3.2",
    config: $cfg
  }}
}' | kctl apply --server-side --force-conflicts -f -

log "Verifying RDMA resources and IB bandwidth between workers"
bash "$IB_SCRIPTS/verify-ib.sh" "$KUBECONFIG_PATH" "$SSH_PRIVATE_KEY" "$workers_json" \
  "$RDMA_RESOURCE_NAME" "$IB_TEST_PAIRS_JSON" "$IB_MIN_BW_MB_PER_SEC" false \
  "$NETWORK_OPERATOR_NAMESPACE"
log "Network operator ready."
