#!/usr/bin/env bash

# Collects what the shared log actions miss, the GPU and NIC cluster policies and
# the node state of every worker, into a directory (default $STATE_DIR/diagnostics).
#
# Best effort, never fails.

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
set +e

out="${1:-$STATE_DIR/diagnostics}"
mkdir -p "$out/nodes"

run() {
  local file="$1"
  shift
  echo "\$ $*" >"$out/$file"
  timeout 300 "$@" >>"$out/$file" 2>&1
}

if [[ -s "$KUBECONFIG_PATH" ]]; then
  export KUBECONFIG="$KUBECONFIG_PATH"
  run gpu-clusterpolicy.yaml kubectl get clusterpolicy -o yaml
  run nic-cluster-policy.yaml kubectl get nicclusterpolicy -o yaml
fi

if [[ -s "$CLUSTER_DATA_PATH" ]]; then
  while IFS= read -r addr; do
    # shellcheck disable=SC2016  # expanded on the remote host
    timeout 120 ssh "${SSH_OPTS[@]}" "$addr" '
      hostname; uptime; echo
      sudo k8s status 2>&1; echo
      nvidia-smi 2>&1 || sudo find /run/nvidia/driver -maxdepth 3 -name nvidia-smi -exec chroot /run/nvidia/driver nvidia-smi \; 2>&1
      echo; ip -br link
      for p in /sys/class/infiniband/*/ports/*; do
        echo "$p state=$(cat "$p/state") rate=$(cat "$p/rate") xmit=$(cat "$p/counters/port_xmit_data") rcv=$(cat "$p/counters/port_rcv_data")"
      done 2>/dev/null
      echo; sudo dmesg -T | tail -200
    ' </dev/null >"$out/nodes/${addr#*@}.txt" 2>&1
  done < <(yq '.["worker-gpu"][].machine_address' "$CLUSTER_DATA_PATH")
fi

log "Diagnostics written to $out"
gh_output path "$out"
exit 0
