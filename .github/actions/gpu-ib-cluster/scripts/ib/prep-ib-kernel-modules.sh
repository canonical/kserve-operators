#!/usr/bin/env bash
# Not persisted across reboots.
set -euo pipefail

private_key_file="$1"
worker_machine_addresses_json="$2"
active_ifnames_json="$3"
disable_acs="${4:-false}"

case "$disable_acs" in
  true|false) ;;
  *) echo "FAIL: disable_acs must be true or false" >&2; exit 1 ;;
esac

ssh_opts=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$private_key_file")

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
remote_script="${script_dir}/remote-prep-ib-kernel-modules.sh"

echo "==> Preparing IB kernel modules on ${worker_machine_addresses_json}"

while IFS= read -r addr; do
  [ -z "$addr" ] && continue
  echo "--- $addr ---"
  ssh_target="$addr"
  [[ "$ssh_target" == *@* ]] || ssh_target="ubuntu@${ssh_target}"
  # shellcheck disable=SC2029
  echo "$active_ifnames_json" | jq -r '.[]' | \
    ssh "${ssh_opts[@]}" "$ssh_target" "$(cat "$remote_script")"
done < <(echo "$worker_machine_addresses_json" | jq -r '.[]')

if [ "$disable_acs" = true ]; then
  bash "${script_dir}/pci-acs.sh" "$private_key_file" \
    "$worker_machine_addresses_json" "$active_ifnames_json" apply
fi

echo "==> IB kernel module prep complete."
