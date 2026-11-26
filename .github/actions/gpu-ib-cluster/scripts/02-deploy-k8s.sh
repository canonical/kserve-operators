#!/usr/bin/env bash

# Bootstraps Canonical K8s on the first worker, joins the others, starts the sshuttle
# tunnels to the K8s API and the load balancer CIDR, and writes the kubeconfig.

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
require_cluster_data

mapfile -t workers < <(jq -r '.[]' <<<"$(worker_addresses_json)")
bootstrap_node="${workers[0]}"
bootstrap_ip="${bootstrap_node#*@}"

# shellcheck disable=SC2029
on() {
  local addr="$1"
  shift
  ssh "${SSH_OPTS[@]}" "$addr" "$@"
}

if on "$bootstrap_node" sudo k8s status </dev/null &>/dev/null; then
  log "$bootstrap_node is already bootstrapped"
else
  group "Bootstrapping $bootstrap_node"
  on "$bootstrap_node" sudo k8s bootstrap --address "$bootstrap_ip" --timeout 10m --file - <<EOF
cluster-config:
  network:
    enabled: true
  dns:
    enabled: true
  load-balancer:
    enabled: true
    cidrs: [$LB_CIDR]
    l2-mode: true
  gateway:
    enabled: true
  local-storage:
    enabled: true
    default: true
EOF
  endgroup
fi

for addr in "${workers[@]:1}"; do
  if on "$addr" sudo k8s status </dev/null &>/dev/null; then
    log "$addr has already joined"
    continue
  fi
  group "Joining $addr"
  name="$(on "$addr" hostname </dev/null)"
  token="$(on "$bootstrap_node" sudo k8s get-join-token "$name" --worker </dev/null)"
  on "$addr" sudo k8s join-cluster "$token" --address "${addr#*@}" --timeout 10m </dev/null
  endgroup
done

group "Waiting for the cluster to be ready"
on "$bootstrap_node" sudo k8s status --wait-ready --timeout 15m </dev/null
endgroup

start_lab_tunnels

on "$bootstrap_node" sudo k8s config </dev/null \
  | SERVER="https://$bootstrap_ip:6443" yq '.clusters[].cluster.server = strenv(SERVER)' >"$KUBECONFIG_PATH"
chmod 600 "$KUBECONFIG_PATH"
kctl wait nodes --all --for=condition=Ready --timeout=15m
kctl get nodes -o wide
log "Kubeconfig written to $KUBECONFIG_PATH"
