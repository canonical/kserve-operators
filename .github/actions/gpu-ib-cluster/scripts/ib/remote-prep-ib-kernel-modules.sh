#!/usr/bin/env bash
# Runs ON each worker machine (piped over SSH by prep-ib-kernel-modules.sh,
# not executed locally) - kept as its own file, rather than an inline
# heredoc, so it gets normal editor/shellcheck/LSP support.
set -euo pipefail

for mod in ib_umad rdma_cm rdma_ucm ib_ipoib; do
  # Each module is loaded with its own modprobe call - combining several
  # modules in one `modprobe a b` call has silently failed to load later
  # ones in this list in prior sessions.
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
