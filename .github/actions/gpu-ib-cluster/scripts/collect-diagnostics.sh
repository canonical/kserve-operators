#!/usr/bin/env bash

# Collects cluster diagnostics into $1 (default $STATE_DIR/diagnostics):
# Juju status/logs of the vllm controller, Kubernetes objects and events, GPU
# and NIC operator state, and per-worker k8s status / nvidia-smi / InfiniBand
# port counters.
# Best effort: never fails.
#
# Outputs:
#   Files under the output directory
#   GITHUB_OUTPUT (if set): path
#   Exit code: 0

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
set +e

out="${1:-$STATE_DIR/diagnostics}"
mkdir -p "$out/nodes"

run() {
  local file="$1"; shift
  echo "\$ $*" > "$out/$file"
  timeout 300 "$@" >> "$out/$file" 2>&1
}

controller="$VLLM_CONTROLLER_NAME"
if juju show-controller "$controller" &>/dev/null; then
  while IFS= read -r model; do
    [[ -n "$model" ]] || continue
    safe="${controller}_${model//\//_}"
    run "juju-status_${safe}.txt" juju status -m "$controller:$model" --relations --storage
    run "juju-debug-log_${safe}.txt" juju debug-log -m "$controller:$model" --replay --no-tail --limit 20000
  done < <(juju models -c "$controller" --format json | jq -r '.models[]."short-name"')
fi

if [[ -s "$KUBECONFIG_PATH" ]]; then
  export KUBECONFIG="$KUBECONFIG_PATH"
  run kubectl-nodes.txt kubectl get nodes -o wide --show-labels
  run kubectl-describe-nodes.txt kubectl describe nodes
  run kubectl-get-all.txt kubectl get all -A -o wide
  run kubectl-events.txt kubectl get events -A --sort-by=.lastTimestamp
  run kubectl-pods-not-running.txt kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded -o wide
  run kubectl-describe-deployments.txt kubectl describe deployments -A
  run kubectl-describe-replicasets.txt kubectl describe replicasets -A
  run kubectl-pvc.txt kubectl get pvc -A
  run gpu-clusterpolicy.yaml kubectl get clusterpolicy -o yaml
  run nic-cluster-policy.yaml kubectl get nicclusterpolicy -o yaml
  run llminferenceservices.yaml kubectl get llminferenceservices -A -o yaml
  run gateways.yaml kubectl get gatewayclass,gateway,httproute -A -o yaml
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
    ' </dev/null > "$out/nodes/${addr#*@}.txt" 2>&1
  done < <(yq '.["worker-gpu"][].machine_address' "$CLUSTER_DATA_PATH")
fi

log "Diagnostics written to $out"
gh_output path "$out"
exit 0
