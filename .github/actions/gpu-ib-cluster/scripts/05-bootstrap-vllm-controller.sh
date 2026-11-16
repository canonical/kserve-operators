#!/usr/bin/env bash

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
require_kubeconfig

if juju controllers --format json 2>/dev/null | jq -e --arg c "$VLLM_CONTROLLER_NAME" '.controllers[$c]' >/dev/null; then
  log "Controller '$VLLM_CONTROLLER_NAME' already exists, skipping bootstrap."
else
  if ! juju clouds --client --format json | jq -e --arg c "$VLLM_CONTROLLER_NAME" 'has($c)' >/dev/null; then
    log "Adding k8s cloud '$VLLM_CONTROLLER_NAME' (storage $VLLM_STORAGE_CLASS)"
    KUBECONFIG="$KUBECONFIG_PATH" juju add-k8s "$VLLM_CONTROLLER_NAME" --client \
      --storage "$VLLM_STORAGE_CLASS"
  fi
  group "Bootstrapping $VLLM_CONTROLLER_NAME"
  juju bootstrap "$VLLM_CONTROLLER_NAME" "$VLLM_CONTROLLER_NAME"
  endgroup
fi

juju switch "$VLLM_CONTROLLER_NAME"
juju wait-for model "$VLLM_CONTROLLER_NAME:controller" --timeout 20m \
  --query='forEach(units, unit => (unit.workload-status == "active"))'
juju controllers
gh_output vllm_controller "$VLLM_CONTROLLER_NAME"
log "Controller '$VLLM_CONTROLLER_NAME' ready."
