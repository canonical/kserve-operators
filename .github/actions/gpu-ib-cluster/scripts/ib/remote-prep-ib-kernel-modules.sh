#!/usr/bin/env bash
# Runs on a worker over SSH (from prep-ib-kernel-modules.sh): loads the RDMA kernel modules and
# brings up the IPoIB interfaces read from stdin, one name per line.
set -euo pipefail

for mod in ib_umad rdma_cm rdma_ucm ib_ipoib; do
  # One modprobe per module: `modprobe a b` silently skipped later ones.
  sudo modprobe "$mod"
done

while IFS= read -r ifname; do
  [ -z "$ifname" ] && continue
  if ip -br link show "$ifname" >/dev/null 2>&1; then
    # ib_ipoib netdevs are created admin-DOWN; bring them up explicitly.
    sudo ip link set "$ifname" up
    echo "  -> brought up $ifname on $(hostname)"
  fi
done
