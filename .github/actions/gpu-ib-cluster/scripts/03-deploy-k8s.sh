#!/usr/bin/env bash

# Creates the Juju model, enlists the GPU workers as manual machines, deploys
# the Canonical K8s charm on them and exports the kubeconfig (what the
# juju_model, juju_add_manual_machines and canonicalk8s units run).
#
# Outputs:
#   $KUBECONFIG_PATH: admin kubeconfig of the cluster
#   sshuttle tunnels to the Juju/K8s APIs and $LB_CIDR (see start_lab_tunnels)
#   Exit code: 0 on success, non-zero on failure

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
require_cluster_data

if ! juju models -c "$JUJU_CONTROLLER_NAME" --format json | jq -e --arg m "$JUJU_MODEL_NAME" '.models[] | select(.["short-name"] == $m)' >/dev/null; then
  log "Adding model $JUJU_MODEL_NAME"
  juju add-model -c "$JUJU_CONTROLLER_NAME" "$JUJU_MODEL_NAME" "$MANUAL_CLOUD_NAME" --no-switch
  juju add-ssh-key -m "$JUJU_MODEL" "$(cat "$SSH_PRIVATE_KEY.pub")"
fi

mapfile -t workers < <(jq -r '.[]' <<<"$(worker_addresses_json)")
machine_ids=()
for addr in "${workers[@]}"; do
  ip="${addr#*@}"
  id="$(juju machines -m "$JUJU_MODEL" --format json \
    | jq -r --arg ip "$ip" '.machines | to_entries[] | select(.value["dns-name"] == $ip or (.value["ip-addresses"] // [] | index($ip))) | .key' | sed -n 1p)"
  if [[ -z "$id" ]]; then
    log "Adding manual machine $addr"
    juju add-machine -m "$JUJU_MODEL" "ssh:$addr" \
      --private-key "$SSH_PRIVATE_KEY" --public-key "$SSH_PRIVATE_KEY.pub"
    id="$(juju machines -m "$JUJU_MODEL" --format json \
      | jq -r --arg ip "$ip" '.machines | to_entries[] | select(.value["dns-name"] == $ip or (.value["ip-addresses"] // [] | index($ip))) | .key' | sed -n 1p)"
    [[ -n "$id" ]] || die "Cannot find Juju machine for $addr"
  fi
  machine_ids+=("$id")
done
log "Workers enlisted as machines: ${machine_ids[*]}"

start_lab_tunnels

if ! juju status -m "$JUJU_MODEL" --format json | jq -e '.applications.k8s' >/dev/null; then
  log "Deploying k8s ($K8S_CHANNEL) to machines ${machine_ids[*]}"
  to="$(IFS=,; echo "${machine_ids[*]}")"
  juju deploy -m "$JUJU_MODEL" k8s --channel "$K8S_CHANNEL" --base "$K8S_BASE" \
    -n "${#machine_ids[@]}" --to "$to" --constraints arch=amd64 \
    --config containerd-custom-registries="$K8S_REGISTRIES" \
    --config load-balancer-cidrs="$LB_CIDR" \
    --config load-balancer-enabled=true \
    --config gateway-enabled=true \
    --config local-storage-enabled=true
  juju expose -m "$JUJU_MODEL" k8s --to-cidrs 0.0.0.0/0
fi

log "Waiting for all units to be active (up to 60m)"
juju wait-for model "$JUJU_MODEL" --timeout 60m --summary \
  --query='forEach(units, unit => unit.workload-status=="active")'

juju run -m "$JUJU_MODEL" k8s/leader get-kubeconfig --format json \
  | jq -r '.[].results.kubeconfig' > "$KUBECONFIG_PATH"
chmod 600 "$KUBECONFIG_PATH"
kctl get nodes -o wide
log "Kubeconfig written to $KUBECONFIG_PATH"
