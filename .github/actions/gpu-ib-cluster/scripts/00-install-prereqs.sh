#!/usr/bin/env bash

# Installs the CLI tools the orchestration needs on this host (juju, kubectl,
# helm, yq, testflinger-cli, jq, sshuttle), the Python venv for the Testflinger
# wrapper, and generates the per-run ephemeral SSH keypair.
#
# Outputs:
#   $STATE_DIR with venv, bin/yq and the ephemeral keypair
#   Exit code: 0 on success, non-zero on failure

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

mkdir -p "$STATE_DIR" "$BIN_DIR"
chmod 700 "$STATE_DIR"

snap_install() {
  local name="$1"; shift
  if snap list "$name" &>/dev/null; then
    sudo snap refresh "$name" "$@"
  else
    sudo snap install "$name" "$@"
  fi
}

group "Installing snaps (juju $JUJU_CHANNEL, kubectl, helm, testflinger-cli)"
snap_install juju --channel="$JUJU_CHANNEL"
# Manual clouds bootstrap over SSH; the strict juju snap needs ~/.ssh access.
sudo snap connect juju:ssh-keys || true
snap_install kubectl --classic --channel="$KUBECTL_CHANNEL"
snap_install helm --classic --channel="$HELM_CHANNEL"
snap_install testflinger-cli
endgroup

group "Installing apt packages (jq, sshuttle, python3-venv)"
sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q jq sshuttle python3-venv openssh-client
endgroup

# Plain binary rather than the strict snap, which cannot read files outside
# $HOME (e.g. the checked-out workspace on some runners).
if ! "$BIN_DIR/yq" --version 2>/dev/null | grep -q "$YQ_VERSION"; then
  log "Installing yq $YQ_VERSION into $BIN_DIR"
  curl -fsSL -o "$BIN_DIR/yq" \
    "https://github.com/mikefarah/yq/releases/download/$YQ_VERSION/yq_linux_amd64"
  chmod +x "$BIN_DIR/yq"
fi

group "Creating Testflinger wrapper venv at $VENV_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$TESTFLINGER_DIR/requirements.txt"
endgroup

if [[ ! -f "$SSH_PRIVATE_KEY" ]]; then
  log "Generating ephemeral SSH keypair $SSH_PRIVATE_KEY"
  ssh-keygen -q -t ed25519 -N "" -C "gpu-ib-cluster-${GITHUB_RUN_ID:-local}" -f "$SSH_PRIVATE_KEY"
fi
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

log "Tool versions:"
juju version
kubectl version --client 2>/dev/null | head -1
helm version --short
yq --version
testflinger-cli --help >/dev/null && echo "testflinger-cli ok"
