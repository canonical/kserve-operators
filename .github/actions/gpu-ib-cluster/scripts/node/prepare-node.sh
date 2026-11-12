#!/usr/bin/env bash

# Node-local preparation, run ON each reserved machine as root by the
# Testflinger job's test phase (embedded into test_data.test_cmds by
# 01-reserve-machines.sh, which also prepends the inputs below). So the
# machines reach the runner already prepared; the cross-node steps
# (bootstrap/join) are done by 02-deploy-k8s.sh.
#
# Inputs (env):
#   K8S_CHANNEL:    k8s snap channel, e.g. 1.34-classic/stable
#   K8S_REGISTRIES: JSON list of {"url": ..., "host": ...} containerd mirrors
#
# Outputs:
#   /var/lib/gpu-ib-cluster/prepared: marker checked by 01-reserve-machines.sh
#   Exit code: 0 on success, non-zero on failure

set -euo pipefail

: "${K8S_CHANNEL:?}" "${K8S_REGISTRIES:?}"
marker_dir=/var/lib/gpu-ib-cluster

echo ">> $(hostname): raising inotify limits"
cat > /etc/sysctl.d/99-increase-inotify-limits.conf <<EOF
fs.inotify.max_user_instances=1024
fs.inotify.max_user_watches=524288
EOF
sysctl --system >/dev/null

echo ">> $(hostname): loading the RDMA modules"
for mod in ib_umad rdma_cm rdma_ucm ib_ipoib; do
  modprobe "$mod" || echo "WARNING: cannot load $mod"
done

# Written before bootstrap/join, as in the k8s-snap air-gapped guide (default
# containerd base dir /etc/containerd).
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
    (( attempt < 3 )) || exit 1
    sleep 30
  done
fi
snap list k8s

mkdir -p "$marker_dir"
date -u +%FT%TZ > "$marker_dir/prepared"
echo ">> $(hostname): prepared"
