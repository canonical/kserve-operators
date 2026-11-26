#!/usr/bin/env bash

# Prints the InfiniBand port state and data counters of every worker.
#
# Best effort, never fails.

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
set +e

[[ -s "$CLUSTER_DATA_PATH" ]] || exit 0
while IFS= read -r addr; do
  group "IB counters on $addr"
  # shellcheck disable=SC2016  # expanded on the remote host
  timeout 120 ssh "${SSH_OPTS[@]}" "$addr" '
    for p in /sys/class/infiniband/*/ports/*; do
      echo "$p state=$(cat "$p/state") rate=$(cat "$p/rate") xmit=$(cat "$p/counters/port_xmit_data") rcv=$(cat "$p/counters/port_rcv_data")"
    done 2>/dev/null
  ' </dev/null 2>&1
  endgroup
done < <(yq '.["worker-gpu"][].machine_address' "$CLUSTER_DATA_PATH")
exit 0
