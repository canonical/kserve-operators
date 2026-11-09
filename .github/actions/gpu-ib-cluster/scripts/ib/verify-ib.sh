#!/usr/bin/env bash
# Runs a real ib_send_bw test between one pod per node, for every pair named
# in the unit's test_pairs. This is what turns "the operator/CR reports
# ready" into "we actually measured RDMA throughput between these specific
# nodes" - adapted from (not vendored from) the run-ib-verify.sh in
# nvidia-network-operator-tf's usage/with-nic-cluster-policy example; expect
# this to evolve once run against real hardware.
#
# Unlike the upstream example, test_pairs here names nodes by POSITION in
# worker_machine_addresses ("worker-1" = index 0, "worker-2" = index 1, ...),
# not by hostname literal - this script resolves each role to a real k8s
# node name itself (via `hostname` over SSH to that machine), so no
# hardware-specific hostnames ever need to appear in catalogue units or
# composites.
set -euo pipefail

kubeconfig_path="$1"
private_key_file="$2"
worker_machine_addresses_json="$3" # JSON array of SSH addresses, ordered
rdma_resource_name="$4"            # e.g. rdma_shared_device_ib (WITHOUT the rdma/ prefix)
test_pairs_json="$5"               # JSON array of {"server_node":"worker-1","client_node":"worker-2"}
min_bw_mb_per_sec="$6"             # ib_send_bw's own unit (MB/sec); 0 = skip the threshold check
keep_pods="$7"                     # "true" or "false"
network_operator_namespace="$8"    # namespace the RDMA shared device plugin daemonset runs in

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Expand a leading "~" ourselves - this runs via a terragrunt after_hook,
# not an interactive login shell, so KUBECONFIG=~/.kube/config would
# otherwise be taken literally.
kubeconfig_path="${kubeconfig_path/#\~/$HOME}"
export KUBECONFIG="$kubeconfig_path"

ssh_opts=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$private_key_file")

# Resolves a "worker-N" (1-indexed) role name to the real k8s node name, by
# SSHing to the Nth address in worker_machine_addresses and asking its
# hostname. Assumes canonical-k8s registers each node under its OS hostname
# (true for the manual-machine setup this composite uses) - flagged as an
# assumption to confirm on real hardware, same as the active_ifnames guess.
resolve_node_name() {
  local role="$1" idx addr
  # Reject anything that isn't a positive-integer "worker-N" - without
  # this, e.g. "worker-0" would silently become jq index -1 (the LAST
  # array entry) instead of failing, verifying the wrong node.
  if [[ ! "$role" =~ ^worker-[1-9][0-9]*$ ]]; then
    echo "FAIL: '$role' is not a valid 'worker-N' (1-indexed, N>=1) role name" >&2
    exit 1
  fi
  idx="${role#worker-}"
  addr=$(echo "$worker_machine_addresses_json" | jq -r ".[$((idx - 1))]")
  if [ -z "$addr" ] || [ "$addr" == "null" ]; then
    echo "FAIL: could not resolve role '$role' to a worker_machine_addresses entry" >&2
    exit 1
  fi
  local ssh_target="$addr"
  [[ "$ssh_target" == *@* ]] || ssh_target="ubuntu@${ssh_target}"
  ssh "${ssh_opts[@]}" "$ssh_target" hostname
}

# test_pairs is a generic unit input - a future caller isn't guaranteed to
# follow the "worker-N" (dash) convention this leaf's own config uses, so a
# role name isn't guaranteed to already be a valid DNS-1123 pod-name segment.
# Warns (rather than silently sanitizing outright) so a mismatched
# convention is still visible in the logs, then sanitizes so the
# verification itself doesn't fail on an otherwise-cosmetic naming issue.
sanitize_dns1123_segment() {
  local raw="$1" sanitized
  sanitized=$(echo "$raw" | tr '[:upper:]_' '[:lower:]-' | sed -E 's/[^a-z0-9-]/-/g; s/^-+//; s/-+$//')
  if [[ "$sanitized" != "$raw" ]]; then
    echo "WARN: role '$raw' is not a valid DNS-1123 pod-name segment as-is - using '$sanitized' for pod naming." >&2
  fi
  echo "$sanitized"
}

# Checked before the daemonset wait below: with no test_pairs configured,
# there is nothing to verify, and the daemonset may not even exist yet
# (e.g. network_operator_config unset) - waiting on it first would make
# this skip path unreachable.
pair_count=$(echo "$test_pairs_json" | jq 'length')
if [ "$pair_count" -eq 0 ]; then
  echo "==> No test_pairs configured - skipping the ib_send_bw verification step."
  exit 0
fi

echo "==> Waiting for the RDMA shared device plugin daemonset to be created..."
# The operator reconciles the just-applied NicClusterPolicy CR
# asynchronously, so the daemonset may not exist yet - "rollout status"
# below fails immediately with NotFound in that case, rather than waiting.
daemonset_found=false
for _ in $(seq 1 60); do
  if kubectl get daemonset/rdma-shared-dp-ds -n "$network_operator_namespace" >/dev/null 2>&1; then
    daemonset_found=true
    break
  fi
  sleep 2
done
if [[ "$daemonset_found" != "true" ]]; then
  echo "FAIL: daemonset/rdma-shared-dp-ds was not created in ${network_operator_namespace} within 120s." >&2
  exit 1
fi

echo "==> Waiting for the RDMA shared device plugin daemonset to roll out..."
kubectl rollout status daemonset/rdma-shared-dp-ds -n "$network_operator_namespace" --timeout=180s

# Runs in a subshell (the outer parens) so an EXIT trap - unlike RETURN -
# fires on every termination path, including an unguarded command failing
# under `set -e` (RETURN only fires on an actual function `return`, not on
# `set -e` killing the process). Each call gets its own fresh subshell, so
# there's no trap/state to reset between pairs either.
run_pair() (
  set -euo pipefail
  local server_role="$1" client_role="$2"
  local server_node client_node pair_id server_pod client_pod server_role_safe client_role_safe

  server_node=$(resolve_node_name "$server_role")
  client_node=$(resolve_node_name "$client_role")
  server_role_safe=$(sanitize_dns1123_segment "$server_role")
  client_role_safe=$(sanitize_dns1123_segment "$client_role")
  pair_id="${server_role_safe}-${client_role_safe}"
  server_pod="ib-verify-server-${pair_id}"
  client_pod="ib-verify-client-${pair_id}"

  echo "==> Pair ${pair_id}: server=${server_node} (${server_role}), client=${client_node} (${client_role})"

  # shellcheck disable=SC2329 # only invoked indirectly, via the trap below
  cleanup() {
    if [[ "$keep_pods" != "true" ]]; then
      kubectl delete pod "$server_pod" "$client_pod" --ignore-not-found --grace-period=0 --force >/dev/null 2>&1 || true
    fi
  }
  trap cleanup EXIT

  render_pod() {
    local pod_name="$1" node_name="$2" role="$3"
    sed \
      -e "s/\${pod_name}/${pod_name}/g" \
      -e "s/\${node_name}/${node_name}/g" \
      -e "s/\${role}/${role}/g" \
      -e "s#\${resource_name_prefixed}#rdma/${rdma_resource_name}#g" \
      "${script_dir}/verify-pod.yaml.tpl"
  }

  echo "==> Deploying verify pods: ${server_pod} (${server_node}), ${client_pod} (${client_node})"
  render_pod "$server_pod" "$server_node" "server" | kubectl apply -f -
  render_pod "$client_pod" "$client_node" "client" | kubectl apply -f -

  echo "==> Waiting for pods to be Running..."
  kubectl wait --for=condition=Ready "pod/${server_pod}" --timeout=120s
  kubectl wait --for=condition=Ready "pod/${client_pod}" --timeout=120s

  echo "==> Installing perftest inside the pods (container-scoped only, not on the hosts)..."
  kubectl exec "$server_pod" -- bash -c "apt-get update -qq && apt-get install -y -qq ibverbs-utils perftest >/tmp/install.log 2>&1"
  kubectl exec "$client_pod" -- bash -c "apt-get update -qq && apt-get install -y -qq ibverbs-utils perftest >/tmp/install.log 2>&1"

  local server_device client_device
  server_device=$(kubectl exec "$server_pod" -- bash -c "ibv_devinfo | awk '/^hca_id:/{print \$2; exit}'")
  client_device=$(kubectl exec "$client_pod" -- bash -c "ibv_devinfo | awk '/^hca_id:/{print \$2; exit}'")
  if [[ -z "$server_device" || -z "$client_device" ]]; then
    echo "FAIL: no RDMA device visible inside one or both pods (ibv_devinfo returned nothing)." >&2
    return 1
  fi
  echo "==> server uses ${server_device}, client uses ${client_device}"

  local server_pod_ip
  server_pod_ip=$(kubectl get pod "$server_pod" -o jsonpath='{.status.podIP}')

  echo "==> Starting ib_send_bw server..."
  kubectl exec "$server_pod" -- bash -c "nohup ib_send_bw -d ${server_device} > /tmp/server.log 2>&1 &"
  sleep 2

  echo "==> Running ib_send_bw client against ${server_pod_ip}..."
  local client_out
  client_out=$(kubectl exec "$client_pod" -- ib_send_bw -d "${client_device}" "${server_pod_ip}" 2>&1) || {
    echo "FAIL: ib_send_bw client run failed:" >&2
    echo "$client_out" >&2
    return 1
  }
  echo "$client_out"

  # Last "#bytes ... BW average[MB/sec] ..." data row, 4th field. The row is
  # indented with leading whitespace, so match on that rather than "^[0-9]".
  local avg_bw
  avg_bw=$(echo "$client_out" | awk '/^[[:space:]]*[0-9]+[[:space:]]/{line=$0} END{split(line,a," "); print a[4]}')
  if [[ -z "$avg_bw" ]]; then
    echo "FAIL: could not parse average bandwidth from ib_send_bw output." >&2
    return 1
  fi

  echo "==> Measured average bandwidth: ${avg_bw} MB/sec (threshold: ${min_bw_mb_per_sec} MB/sec)"
  # awk (already required above for parsing), not bc, for the float
  # comparison - bc isn't provisioned on the deployment runner.
  if awk -v bw="$avg_bw" -v thr="$min_bw_mb_per_sec" 'BEGIN { exit !(thr > 0 && bw < thr) }'; then
    echo "FAIL: measured bandwidth ${avg_bw} MB/sec is below the ${min_bw_mb_per_sec} MB/sec threshold." >&2
    return 1
  fi

  echo "==> PASS: InfiniBand verified between ${server_node} and ${client_node} (${avg_bw} MB/sec)."
)

for i in $(seq 0 $((pair_count - 1))); do
  server_role=$(echo "$test_pairs_json" | jq -r ".[$i].server_node")
  client_role=$(echo "$test_pairs_json" | jq -r ".[$i].client_node")
  run_pair "$server_role" "$client_role"
done
