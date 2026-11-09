#!/usr/bin/env bash
# Discovers, applies or checks scoped PCI ACS mitigation on bare-metal workers.
# Args: SSH key, JSON worker addresses, JSON active NIC names, preview|apply|check.
set -euo pipefail

private_key_file="${1:?SSH private key required}"
addresses_json="${2:?worker addresses required}"
ifnames_json="${3:?active NIC names required}"
mode="${4:?preview, apply or check required}"

case "$mode" in
  preview|apply|check) ;;
  *) echo "FAIL: invalid ACS mode: $mode" >&2; exit 1 ;;
esac

addresses=$(jq -er '
  if type == "array" and length > 0 and all(.[]; type == "string" and test("^[a-zA-Z0-9_.:@-]+$"))
  then .[] else error("expected non-empty worker addresses") end
' <<<"$addresses_json")
ifnames=$(jq -er '
  if type == "array" and length > 0 and all(.[]; type == "string" and test("^[a-zA-Z0-9_.:-]+$"))
  then .[] else error("expected non-empty NIC names") end
' <<<"$ifnames_json")

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
printf -v remote_command 'bash -s -- %q' "$mode"
while IFS= read -r ifname; do
  printf -v remote_command '%s %q' "$remote_command" "$ifname"
done <<<"$ifnames"

while IFS= read -r address; do
  [[ "$address" == *@* ]] || address="ubuntu@$address"
  echo "==> PCI ACS $mode on $address"
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o BatchMode=yes -o ConnectTimeout=10 -i "$private_key_file" \
    "$address" "$remote_command" <"${script_dir}/remote-pci-acs.sh"
done <<<"$addresses"
