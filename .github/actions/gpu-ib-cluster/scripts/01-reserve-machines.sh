#!/usr/bin/env bash

# Reserves the $CLUSTER machines on Testflinger (GPU workers). The job's test
# phase runs before reserve, on the agent host that can already reach the
# device ($DEVICE_IP), and:
#   - injects the per-run ephemeral public key;
#   - runs node/prepare-node.sh on the device (inotify limits, RDMA modules,
#     containerd mirrors, k8s snap).
# Humans get access through SSH_IMPORT_IDS, via reserve_data.ssh_keys.
# Blocks until all reservations are ready (can take hours if agents are busy),
# then checks SSH access and that every machine was prepared.
#
# Requires TESTFLINGER_CLIENT_ID / TESTFLINGER_SECRET_KEY in CI (job_priority
# overrides); without them jobs are submitted at default priority.
#
# Outputs:
#   $JOB_DATA_PATH: every accepted job (written as soon as jobs are submitted,
#                   so 99-release.sh can cancel them even if this script fails)
#   $CLUSTER_DATA_PATH: machine addresses per role
#   GITHUB_OUTPUT (if set): workers
#   Exit code: 0 on success, non-zero on failure

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

if [[ -s "$JOB_DATA_PATH" ]]; then
  die "$JOB_DATA_PATH exists: a reservation may still be active. Run 99-release.sh first."
fi
[[ -f "$SSH_PRIVATE_KEY.pub" ]] || die "Missing $SSH_PRIVATE_KEY.pub; run 00-install-prereqs.sh first"

read -r -a import_ids <<<"${SSH_IMPORT_IDS//,/ }"
[[ ${#import_ids[@]} -gt 0 ]] || die "SSH_IMPORT_IDS is empty: Testflinger reservations need at least one lp:/gh: identity"
import_ids_json="$(printf '%s\n' "${import_ids[@]}" | jq -R . | jq -cs .)"

# The node script with its inputs prepended.
node_script="$(
  printf 'export K8S_CHANNEL=%q\nexport K8S_REGISTRIES=%q\n' "$K8S_CHANNEL" "$K8S_REGISTRIES"
  cat "$SCRIPTS_DIR/node/prepare-node.sh"
)"
grep -qx PREPARE_NODE_EOF <<<"$node_script" && die "node/prepare-node.sh must not contain a PREPARE_NODE_EOF line"

# The test phase runs on the agent host, which reaches the device as ubuntu.
test_cmds="$(cat <<EOF
set -e
ssh_device() { ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ubuntu@\$DEVICE_IP "\$@"; }
ssh_device "mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '$(cat "$SSH_PRIVATE_KEY.pub")' >> ~/.ssh/authorized_keys"
echo "Injected ephemeral key into \$DEVICE_IP"
ssh_device "sudo bash -s" <<'PREPARE_NODE_EOF'
$node_script
PREPARE_NODE_EOF
EOF
)"

job_config="$STATE_DIR/job-config.yaml"
IMPORT_IDS="$import_ids_json" TEST_CMDS="$test_cmds" yq "
  .reserve_data.ssh_keys = (.reserve_data.ssh_keys + (strenv(IMPORT_IDS) | from_json) | unique) |
  .reserve_data.timeout = $RESERVATION_TIMEOUT_SECONDS |
  .test_data.test_cmds = strenv(TEST_CMDS)
" "$TESTFLINGER_DIR/job-configs/default.yaml" > "$job_config"
log "Job config ($job_config):"
cat "$job_config"

overrides_args=()
if [[ "${JOB_PRIORITY_OVERRIDES:-false}" == true ]]; then
  [[ -n "${TESTFLINGER_CLIENT_ID:-}" && -n "${TESTFLINGER_SECRET_KEY:-}" ]] \
    || die "JOB_PRIORITY_OVERRIDES=true needs TESTFLINGER_CLIENT_ID and TESTFLINGER_SECRET_KEY"
  overrides_args=(--agent-job-overrides-path "$TESTFLINGER_DIR/job-configs/agent-job-overrides.yaml")
fi

group "Reserving cluster '$CLUSTER' (series $SERIES)"
PYTHONUNBUFFERED=1 "$VENV_DIR/bin/python" "$TESTFLINGER_DIR/deploy_testflinger_cluster.py" \
  "$TESTFLINGER_DIR/cluster-specs/$CLUSTER.yaml" \
  "$job_config" \
  "${overrides_args[@]}" \
  --series "$SERIES" \
  --first-wave-timeout-seconds 0 \
  --second-wave-timeout-seconds "$ACQUIRE_TIMEOUT_SECONDS" \
  --output-filepath "$JOB_DATA_PATH" \
  --cluster-output-filepath "$CLUSTER_DATA_PATH"
endgroup

log "Job data:"; cat "$JOB_DATA_PATH"
log "Cluster data:"; cat "$CLUSTER_DATA_PATH"
gh_output workers "$(worker_addresses_json)"

group "Checking SSH access and the node preparation"
while IFS= read -r addr; do
  ip="${addr#*@}"
  ssh-keygen -R "$ip" &>/dev/null || true
  ssh-keyscan -T 15 "$ip" >> ~/.ssh/known_hosts 2>/dev/null || die "Cannot reach $addr from this runner"
  ssh "${SSH_OPTS[@]}" "$addr" 'echo "$(hostname): prepared at $(cat /var/lib/gpu-ib-cluster/prepared)"' </dev/null \
    || die "$addr: SSH with the ephemeral key failed or node/prepare-node.sh did not complete; see the test phase in the Testflinger job log ($(yq ".[] | select(.IP == \"$ip\") | .job_id" "$JOB_DATA_PATH"))"
done < <(machine_addresses)
endgroup

log "Reservation ready."
