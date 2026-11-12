#!/usr/bin/env bash

# Releases the Testflinger reservations (machines are reprovisioned, so no
# Juju/K8s teardown is needed) and cleans the local state: sshuttle tunnels,
# ~/.ssh/config block, Juju client entries, kubeconfig and the ephemeral key.
# Best effort: never fails, so it is safe in `if: always()` steps.
# RELEASE_MACHINES=false keeps the reservations (debugging) and leaves the
# local state untouched (state dir with kubeconfig, Juju client entries,
# sshuttle tunnels), e.g. for a tmate session at the end of the job.
#
# Outputs:
#   stdout: progress
#   Exit code: 0

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
set +e

if [[ "${RELEASE_MACHINES:-true}" != "true" ]]; then
  log "RELEASE_MACHINES=${RELEASE_MACHINES}: keeping the Testflinger reservations and the local state in $STATE_DIR"
  exit 0
fi

if [[ -s "$JOB_DATA_PATH" ]]; then
  log "Cancelling Testflinger jobs"
  while IFS= read -r job_id; do
    [[ -n "$job_id" && "$job_id" != "null" ]] || continue
    testflinger-cli cancel "$job_id" || echo "Cannot cancel $job_id (already finished?)"
  done < <(yq '.[].job_id' "$JOB_DATA_PATH")
else
  log "No job data at $JOB_DATA_PATH: nothing to cancel"
fi

log "Stopping sshuttle tunnels"
for pidfile in "$STATE_DIR"/sshuttle-*.pid; do
  [[ -f "$pidfile" ]] || continue
  kill "$(cat "$pidfile")" 2>/dev/null || sudo kill "$(cat "$pidfile")" 2>/dev/null
  rm -f "$pidfile"
done

log "Removing aproxy bypass rules"
while read -r handle; do
  sudo nft delete rule ip aproxy output handle "$handle"
done < <(sudo nft -a list chain ip aproxy output 2>/dev/null \
  | grep -F "comment \"$APROXY_RULE_COMMENT\"" | sed -n 's/.*# handle \([0-9]*\)$/\1/p')

log "Removing ~/.ssh/config block"
if [[ -f ~/.ssh/config ]]; then
  sed -i "\|^# BEGIN gpu-ib-cluster $STATE_DIR\$|,\|^# END gpu-ib-cluster $STATE_DIR\$|d" ~/.ssh/config
fi

log "Removing Juju client entries"
for controller in "$VLLM_CONTROLLER_NAME" "$JUJU_CONTROLLER_NAME"; do
  juju unregister -y "$controller" 2>/dev/null
done
juju remove-cloud --client "$VLLM_CONTROLLER_NAME" 2>/dev/null
juju remove-cloud --client "$MANUAL_CLOUD_NAME" 2>/dev/null

if [[ -d "$STATE_DIR" ]]; then
  while IFS= read -r addr; do
    ssh-keygen -R "${addr#*@}" &>/dev/null
  done < <([[ -s "$JOB_DATA_PATH" ]] && machine_addresses)
  log "Removing local state $STATE_DIR"
  rm -rf "$STATE_DIR"
fi

log "Released."
exit 0
