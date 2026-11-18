#!/usr/bin/env bash

# Runs as root on each machine in the Testflinger test phase; 01-reserve-machines.sh prepends the inputs.

set -euo pipefail

: "${K8S_CHANNEL:?}" "${K8S_REGISTRIES:?}"
marker_dir=/var/lib/gpu-ib-cluster

echo ">> $(hostname): raising inotify limits"
cat >/etc/sysctl.d/99-increase-inotify-limits.conf <<EOF
fs.inotify.max_user_instances=1024
fs.inotify.max_user_watches=524288
EOF
sysctl --system >/dev/null

echo ">> $(hostname): loading the RDMA modules"
for mod in ib_umad rdma_cm rdma_ucm ib_ipoib; do
  modprobe "$mod" || echo "WARNING: cannot load $mod"
done

# Before bootstrap/join, as in the k8s-snap air-gapped guide.
echo ">> $(hostname): writing the containerd registry mirrors"
python3 - <<'EOF'
import json, os
for reg in json.loads(os.environ["K8S_REGISTRIES"]):
    d = os.path.join("/etc/containerd/hosts.d", reg["host"])
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "hosts.toml"), "w") as f:
        f.write(f'server = "{reg["url"]}"\n\n[host."{reg["url"]}"]\ncapabilities = ["pull", "resolve"]\n')
    print(f'{reg["host"]} -> {reg["url"]}')
EOF

if snap list k8s &>/dev/null; then
  echo ">> $(hostname): k8s snap already installed"
else
  echo ">> $(hostname): installing the k8s snap ($K8S_CHANNEL)"
  for attempt in 1 2 3; do
    snap install k8s --classic --channel "$K8S_CHANNEL" && break
    ((attempt < 3)) || exit 1
    sleep 30
  done
fi
snap list k8s

mkdir -p "$marker_dir"
date -u +%FT%TZ >"$marker_dir/prepared"
echo ">> $(hostname): prepared"
