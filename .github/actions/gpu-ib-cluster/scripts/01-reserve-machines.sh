#!/usr/bin/env bash

# Reserves the $CLUSTER machines on Testflinger (controller + GPU workers) and
# prepares the hosts. Access is granted to:
#   - the per-run ephemeral key, injected by the job's test phase (which runs
#     before reserve, from the agent host that can already reach the device);
#   - the human identities in SSH_IMPORT_IDS, via reserve_data.ssh_keys.
# Blocks until all reservations are ready (can take hours if agents are busy).
#
# Requires TESTFLINGER_CLIENT_ID / TESTFLINGER_SECRET_KEY in CI (job_priority
# overrides); without them jobs are submitted at default priority.
#
# Outputs:
#   $JOB_DATA_PATH: every accepted job (written as soon as jobs are submitted,
#                   so 99-release.sh can cancel them even if this script fails)
#   $CLUSTER_DATA_PATH: machine addresses per role
#   GITHUB_OUTPUT (if set): controller, workers
#   Exit code: 0 on success, non-zero on failure

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

if [[ -s "$JOB_DATA_PATH" ]]; then
  die "$JOB_DATA_PATH exists: a reservation may still be active. Run 99-release.sh first."
fi
[[ -f "$SSH_PRIVATE_KEY.pub" ]] || die "Missing $SSH_PRIVATE_KEY.pub; run 00-install-prereqs.sh first"

read -r -a import_ids <<<"${SSH_IMPORT_IDS//,/ }"
[[ ${#import_ids[@]} -gt 0 ]] || die "SSH_IMPORT_IDS is empty: Testflinger reservations need at least one lp:/gh: identity"
import_ids_json="$(printf '%s\n' "${import_ids[@]}" | jq -R . | jq -cs .)"

# The test phase runs on the agent host, which reaches the device as ubuntu.
inject_cmds="$(cat <<EOF
set -e
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ubuntu@\$DEVICE_IP \\
  "mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '$(cat "$SSH_PRIVATE_KEY.pub")' >> ~/.ssh/authorized_keys"
echo "Injected ephemeral key into \$DEVICE_IP"
EOF
)"

job_config="$STATE_DIR/job-config.yaml"
IMPORT_IDS="$import_ids_json" INJECT_CMDS="$inject_cmds" yq "
  .reserve_data.ssh_keys = (.reserve_data.ssh_keys + (strenv(IMPORT_IDS) | from_json) | unique) |
  .reserve_data.timeout = $RESERVATION_TIMEOUT_SECONDS |
  .test_data.test_cmds = strenv(INJECT_CMDS)
" "$TESTFLINGER_DIR/job-configs/default.yaml" > "$job_config"
log "Job config ($job_config):"
cat "$job_config"

overrides_args=()
if [[ -n "${TESTFLINGER_CLIENT_ID:-}" && -n "${TESTFLINGER_SECRET_KEY:-}" ]]; then
  overrides_args=(--agent-job-overrides-path "$TESTFLINGER_DIR/job-configs/agent-job-overrides.yaml")
else
  log "No Testflinger credentials: submitting without job_priority overrides"
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
gh_output controller "$(controller_address)"
gh_output workers "$(worker_addresses_json)"

# `juju bootstrap` (manual cloud) has no --private-key flag: it relies on the
# default SSH identities, so scope the ephemeral key to the reserved hosts.
ssh_config_begin="# BEGIN gpu-ib-cluster $STATE_DIR"
ssh_config_end="# END gpu-ib-cluster $STATE_DIR"
touch ~/.ssh/config && chmod 600 ~/.ssh/config
{
  echo "$ssh_config_begin"
  echo "Host $(machine_addresses | sed 's/.*@//' | tr '\n' ' ')"
  echo "  IdentityFile $SSH_PRIVATE_KEY"
  echo "  IdentitiesOnly no"
  echo "$ssh_config_end"
} > "$STATE_DIR/ssh-config-block"
cat "$STATE_DIR/ssh-config-block" ~/.ssh/config > "$STATE_DIR/ssh-config.new"
cat "$STATE_DIR/ssh-config.new" > ~/.ssh/config
rm -f "$STATE_DIR/ssh-config.new"

group "Checking SSH access and preparing hosts"
while IFS= read -r addr; do
  ip="${addr#*@}"
  ssh-keygen -R "$ip" &>/dev/null || true
  ssh-keyscan -T 15 "$ip" >> ~/.ssh/known_hosts 2>/dev/null || die "Cannot reach $addr from this runner"

  # Raise inotify limits and load the non-persistent RDMA modules.
  ssh "${SSH_OPTS[@]}" "$addr" '
    sudo tee /etc/sysctl.d/99-increase-inotify-limits.conf >/dev/null <<EOF
fs.inotify.max_user_instances=1024
fs.inotify.max_user_watches=524288
EOF
    sudo sysctl --system >/dev/null
    for mod in ib_umad rdma_cm rdma_ucm ib_ipoib; do
      sudo modprobe "$mod" || true
    done
    echo "$(hostname): ready"
  ' </dev/null || die "SSH with the ephemeral key failed for $addr (key injection via test_cmds?)"
done < <(machine_addresses)
endgroup

log "Reservation ready."
