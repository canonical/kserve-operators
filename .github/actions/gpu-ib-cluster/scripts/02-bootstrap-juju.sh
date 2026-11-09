#!/usr/bin/env bash

# Bootstraps a Juju controller on the reserved controller machine as a manual
# cloud.
#
# Outputs:
#   Juju controller $JUJU_CONTROLLER_NAME registered in the local Juju client
#   Exit code: 0 on success, non-zero on failure

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
require_cluster_data

controller_addr="$(controller_address)"
[[ "$controller_addr" == *@* ]] || die "No controller address in $CLUSTER_DATA_PATH"

if juju controllers --format json 2>/dev/null | jq -e --arg c "$JUJU_CONTROLLER_NAME" '.controllers[$c]' >/dev/null; then
  log "Controller '$JUJU_CONTROLLER_NAME' already exists, skipping bootstrap."
  log "If it is stale (old reservation), run: juju unregister -y $JUJU_CONTROLLER_NAME"
else
  cloud_file="$STATE_DIR/manual-cloud.yaml"
  cat > "$cloud_file" <<YAML
clouds:
  $MANUAL_CLOUD_NAME:
    type: manual
    auth-types: []
    endpoint: $controller_addr
YAML
  if juju clouds --client --format json | jq -e --arg c "$MANUAL_CLOUD_NAME" 'has($c)' >/dev/null; then
    juju update-cloud "$MANUAL_CLOUD_NAME" -f "$cloud_file" --client
  else
    juju add-cloud "$MANUAL_CLOUD_NAME" -f "$cloud_file" --client
  fi
  log "Bootstrapping $JUJU_CONTROLLER_NAME on $controller_addr"
  juju bootstrap "$MANUAL_CLOUD_NAME" "$JUJU_CONTROLLER_NAME"
fi

juju wait-for model "$JUJU_CONTROLLER_NAME:controller" --timeout 3600s \
  --query='forEach(units, unit => (unit.workload-status == "active"))'
log "Juju controller ready."
