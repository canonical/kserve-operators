#!/usr/bin/env bash

# Shared configuration for the gpu-ib-cluster scripts. Sourced by every script
# in this directory; override any value via env vars (the composite actions
# map their inputs onto these).
#
# Topology (Testflinger cluster spec tor-A100_x2_IB):
#   controller[0] (akis)            -> Juju manual-cloud controller only
#   worker-gpu[0..1] (cortez/blanka) -> Canonical K8s nodes (GPUs + InfiniBand)
#   this host (GH runner)            -> orchestrator: juju/kubectl/helm client,
#                                       sshuttle to the LB CIDR, tests

# SSH_OPTS is consumed by the scripts that source this file.
# shellcheck disable=SC2034

set -euo pipefail

ACTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ACTION_DIR
export SCRIPTS_DIR="$ACTION_DIR/scripts"
export IB_SCRIPTS="$SCRIPTS_DIR/ib"
export TESTFLINGER_DIR="$ACTION_DIR/testflinger"

export CLUSTER="${CLUSTER:-tor-A100_x2_IB}"
export SERIES="${SERIES:-noble}"
export JUJU_CHANNEL="${JUJU_CHANNEL:-3.6/stable}"
export KUBECTL_CHANNEL="${KUBECTL_CHANNEL:-latest/stable}"
export HELM_CHANNEL="${HELM_CHANNEL:-latest/stable}"
export YQ_VERSION="${YQ_VERSION:-v4.44.3}"

# Human access: space/comma separated ssh-import-id identities (lp:, gh:)
# added to the reservation, on top of the per-run ephemeral key.
export SSH_IMPORT_IDS="${SSH_IMPORT_IDS:-}"
export RESERVATION_TIMEOUT_SECONDS="${RESERVATION_TIMEOUT_SECONDS:-21600}"
# Max time to wait for the agents to become free and reach "reserve".
export ACQUIRE_TIMEOUT_SECONDS="${ACQUIRE_TIMEOUT_SECONDS:-154800}"

# Local state. It must live under $HOME and not in a hidden dir: the strictly
# confined juju snap can only read non-hidden files in $HOME.
export STATE_DIR="${STATE_DIR:-$HOME/gpu-ib-cluster-state-${GITHUB_RUN_ID:-local}${GITHUB_RUN_ATTEMPT:+-$GITHUB_RUN_ATTEMPT}}"
export BIN_DIR="$STATE_DIR/bin"
export JOB_DATA_PATH="$STATE_DIR/testflinger-job-data.yaml"
export CLUSTER_DATA_PATH="$STATE_DIR/testflinger-cluster-data.yaml"
export KUBECONFIG_PATH="$STATE_DIR/kubeconfig"
export VENV_DIR="$STATE_DIR/venv"
export SSHUTTLE_PIDFILE="$STATE_DIR/sshuttle.pid"
# Marker of the ~/.ssh/authorized_keys block written by grant-runner-access.sh.
export RUNNER_ACCESS_MARKER="gpu-ib-cluster runner access $STATE_DIR"
# Touch this file on the runner to end a hold-runner step early.
export HOLD_RELEASE_FILE="$HOME/gpu-ib-continue"
# Ephemeral keypair generated per run by 00-install-prereqs.sh.
export SSH_PRIVATE_KEY="${SSH_PRIVATE_KEY:-$STATE_DIR/id_ed25519}"

export PATH="$BIN_DIR:$PATH"

# Juju names.
export JUJU_CONTROLLER_NAME="${JUJU_CONTROLLER_NAME:-controller-manual-cloud}"
export MANUAL_CLOUD_NAME="${MANUAL_CLOUD_NAME:-manual-cloud}"
export JUJU_MODEL_NAME="${JUJU_MODEL_NAME:-canonicalk8s-gpu-net}"
export JUJU_MODEL="$JUJU_CONTROLLER_NAME:$JUJU_MODEL_NAME"
export VLLM_CONTROLLER_NAME="${VLLM_CONTROLLER_NAME:-vllm}"
export VLLM_STORAGE_CLASS="${VLLM_STORAGE_CLASS:-csi-rawfile-default}"

# Canonical K8s (k8s charm) settings for the tor lab.
export K8S_CHANNEL="${K8S_CHANNEL:-1.34/stable}"
export K8S_BASE="${K8S_BASE:-ubuntu@24.04}"
export LB_CIDR="${LB_CIDR:-10.243.10.0/24}"
export K8S_REGISTRIES="${K8S_REGISTRIES:-[{ \"url\": \"http://10.141.186.140:5000\", \"host\": \"docker.io\" }, {\"url\": \"http://10.141.186.145:5000\", \"host\": \"registry.k8s.io\"}]}"

export NVIDIA_HELM_REPO="${NVIDIA_HELM_REPO:-https://helm.ngc.nvidia.com/nvidia}"
export GPU_OPERATOR_VERSION="${GPU_OPERATOR_VERSION:-v26.7.2}"
export GPU_OPERATOR_NAMESPACE="gpu-operator"
export NETWORK_OPERATOR_VERSION="${NETWORK_OPERATOR_VERSION:-26.7.0}"
export NETWORK_OPERATOR_NAMESPACE="network-operator"

export IB_IFNAMES_JSON="${IB_IFNAMES_JSON:-[\"ibp18s0\"]}"
export DISABLE_ACS="${DISABLE_ACS:-true}"
export RDMA_RESOURCE_NAME="${RDMA_RESOURCE_NAME:-rdma_shared_device_ib}"
export RDMA_HCA_MAX="${RDMA_HCA_MAX:-63}"
export IB_TEST_PAIRS_JSON="${IB_TEST_PAIRS_JSON:-[{\"server_node\":\"worker-1\",\"client_node\":\"worker-2\"}]}"
export IB_MIN_BW_MB_PER_SEC="${IB_MIN_BW_MB_PER_SEC:-1000}"

SSH_OPTS=(-i "$SSH_PRIVATE_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes)

if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
  log() { echo ">> $*"; }
  die() { echo "::error::$*" >&2; exit 1; }
  group() { echo "::group::$*"; }
  endgroup() { echo "::endgroup::"; }
else
  log() { echo -e "\033[1;34m>> $*\033[0m"; }
  die() { echo -e "\033[1;31mERROR: $*\033[0m" >&2; exit 1; }
  group() { log "$*"; }
  endgroup() { :; }
fi

require_cluster_data() {
  [[ -s "$CLUSTER_DATA_PATH" ]] || die "No cluster data at $CLUSTER_DATA_PATH; run 01-reserve-machines.sh first"
}

require_kubeconfig() {
  [[ -s "$KUBECONFIG_PATH" ]] || die "No kubeconfig at $KUBECONFIG_PATH; run 03-deploy-k8s.sh first"
}

# All reserved machines as user@ip, one per line.
machine_addresses() {
  yq '.[] | select(has("IP")) | .username + "@" + .IP' "$JOB_DATA_PATH"
}

controller_address() {
  yq '.controller[0].machine_address' "$CLUSTER_DATA_PATH"
}

# Worker addresses (user@ip) as a JSON array, in cluster-spec order.
worker_addresses_json() {
  yq -o json -I0 '[.["worker-gpu"][].machine_address]' "$CLUSTER_DATA_PATH"
}

kctl() { kubectl --kubeconfig "$KUBECONFIG_PATH" "$@"; }

# Appends key=value to $GITHUB_OUTPUT when running in CI.
gh_output() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "$1=$2" >> "$GITHUB_OUTPUT"
  fi
}
