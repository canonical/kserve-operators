#!/usr/bin/env bash

# Keeps the job (and so the runner, with its Juju/kubectl state and sshuttle
# tunnel) alive for $1 minutes, so the caller can SSH in and debug. Ends early
# when $HOLD_RELEASE_FILE exists on the runner.
#
# Outputs:
#   stdout: how to log in and how to end the hold
#   Exit code: 0

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

minutes="${1:?hold duration in minutes required}"
[[ "$minutes" =~ ^[0-9]+$ ]] || die "Hold duration must be a whole number of minutes"
(( minutes > 0 )) || exit 0

addr="$(whoami)@$(hostname -I | awk '{print $1}')"
msg="Holding the runner for ${minutes}m: ssh $addr (export KUBECONFIG=$KUBECONFIG_PATH). End early with: touch $HOLD_RELEASE_FILE"
log "$msg"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  echo -e "### Runner on hold\n\n$msg" >> "$GITHUB_STEP_SUMMARY"
fi

rm -f "$HOLD_RELEASE_FILE"
deadline=$(( $(date +%s) + minutes * 60 ))
while (( $(date +%s) < deadline )); do
  if [[ -e "$HOLD_RELEASE_FILE" ]]; then
    log "$HOLD_RELEASE_FILE found: ending hold"
    break
  fi
  sleep 30
done
rm -f "$HOLD_RELEASE_FILE"
exit 0
