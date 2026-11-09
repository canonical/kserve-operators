#!/usr/bin/env bash
# Runs on a worker via pci-acs.sh. Only PLX/PEX880 GPU/NIC ancestors are eligible.
set -euo pipefail
shopt -s nullglob

fail() { echo "FAIL: $*" >&2; exit 1; }
mode="${1:?preview, apply or check required}"
shift
case "$mode" in
  preview|apply|check) ;;
  *) fail "invalid ACS mode: $mode" ;;
esac
[ "$#" -gt 0 ] || fail "at least one active NIC is required"

# Alternate roots allow fixture tests without PCI hardware or privileged writes.
sysfs="${ACS_SYSFS_ROOT:-/sys}"
procfs="${ACS_PROC_ROOT:-/proc}"
bdf_pattern='^[[:xdigit:]]{4}:[[:xdigit:]]{2}:[[:xdigit:]]{2}\.[0-7]$'
for tool in systemd-detect-virt readlink lspci setpci sort sudo; do
  command -v "$tool" >/dev/null || fail "required tool not found: $tool"
done
if virtualization=$(systemd-detect-virt --vm); then
  fail "ACS mitigation requires bare metal, detected $virtualization"
else
  rc=$?
  [ "$rc" -eq 1 ] && [ "$virtualization" = none ] || fail "cannot establish bare-metal status"
fi
for device in "$sysfs"/bus/pci/devices/*; do
  if [ -L "$device/driver" ] && [ "$(basename "$(readlink -f "$device/driver")")" = vfio-pci ]; then
    fail "VFIO-bound device present; ACS disablement is incompatible with passthrough"
  fi
done

boot_id=$(cat "$procfs/sys/kernel/random/boot_id")
[ -n "$boot_id" ] || fail "missing boot ID"
echo "ACS_BOOT_ID=$boot_id"
bridges=()
gpu_count=0

walk_endpoint() {
  local endpoint="$1" kind="$2" path bdf class identity matches=0
  path=$(readlink -f "$endpoint")
  [[ "$path" == "$sysfs"/devices/* ]] || fail "$kind has no PCI device path: $endpoint"
  bdf="${path##*/}"
  [[ "$bdf" =~ $bdf_pattern ]] || fail "invalid $kind PCI endpoint: $path"
  echo "ACS_ENDPOINT $kind $bdf"
  path="${path%/*}"
  while [[ "${path##*/}" =~ $bdf_pattern ]]; do
    bdf="${path##*/}"
    class=$(cat "$path/class")
    [ "$class" = 0x060400 ] || fail "unexpected non-bridge ancestor $bdf ($class)"
    identity=$(lspci -D -s "$bdf")
    [ -n "$identity" ] || fail "missing PCI identity for $bdf"
    if [[ "$identity" =~ PEX[[:space:]]*880|PLX ]]; then
      echo "ACS_PATH $kind -> $bdf ($identity)"
      bridges+=("$bdf")
      matches=$((matches + 1))
    fi
    path="${path%/*}"
  done
  [ "$matches" -gt 0 ] || fail "no supported PLX/PEX880 bridge on $kind path"
}

nic_count=0
for ifname in "$@"; do
  [[ "$ifname" =~ ^[a-zA-Z0-9_.:-]+$ ]] && [ "$ifname" != . ] && [ "$ifname" != .. ] \
    || fail "invalid NIC name: $ifname"
  if [ ! -e "$sysfs/class/net/$ifname" ] && [ ! -L "$sysfs/class/net/$ifname" ]; then
    echo "ACS_SKIP NIC:$ifname (not present on this worker)"
    continue
  fi
  endpoint=$(readlink -f "$sysfs/class/net/$ifname/device") || fail "NIC $ifname not found (are RDMA modules loaded?)"
  [ -r "$endpoint/class" ] || fail "missing PCI endpoint for NIC $ifname"
  nic_class=$(cat "$endpoint/class")
  [[ "$nic_class" == 0x02* ]] || fail "$ifname is not a PCI network device"
  walk_endpoint "$endpoint" "NIC:$ifname"
  nic_count=$((nic_count + 1))
done
[ "$nic_count" -gt 0 ] || fail "no configured NIC endpoints found on this worker (are RDMA modules loaded?)"
for device in "$sysfs"/bus/pci/devices/*; do
  vendor=$(cat "$device/vendor")
  class=$(cat "$device/class")
  if [ "$vendor" = 0x10de ] && [[ "$class" == 0x03* ]]; then
    walk_endpoint "$device" GPU
    gpu_count=$((gpu_count + 1))
  fi
done
[ "$gpu_count" -gt 0 ] || fail "no NVIDIA GPU endpoints found"

unique_bridges=$(printf '%s\n' "${bridges[@]}" | sort -u)
bridges=()
before=()
while IFS= read -r bdf; do
  [[ "$bdf" =~ $bdf_pattern ]] || fail "invalid bridge BDF: $bdf"
  # Upstream switch ports typically lack ACS; only skip when lspci positively shows no ACS capability.
  capabilities=$(sudo -n lspci -vvv -s "$bdf") || fail "cannot read capabilities of $bdf"
  if grep -qi '<access denied>' <<<"$capabilities"; then
    fail "cannot read capabilities of $bdf: access denied"
  fi
  if ! grep -q 'Access Control Services' <<<"$capabilities"; then
    echo "ACS_SKIP $bdf (no ACS capability)"
    continue
  fi
  control=$(sudo -n setpci -s "$bdf" ECAP_ACS+0x6.w)
  [[ "$control" =~ ^[[:xdigit:]]{4}$ ]] || fail "invalid ACS control for $bdf: $control"
  bridges+=("$bdf")
  before+=("$control")
  echo "ACS_BEFORE $boot_id $bdf $control"
  printf 'ACS_COMMAND sudo -n setpci -s %s ECAP_ACS+0x6.w=0000\n' "$bdf"
done <<<"$unique_bridges"

# Finish discovery and capture every original control word before the first write.
for i in "${!bridges[@]}"; do
  bdf="${bridges[$i]}"
  if [ "$mode" = apply ] && [ "${before[$i]}" != 0000 ]; then
    sudo -n setpci -s "$bdf" ECAP_ACS+0x6.w=0000
  fi
  if [ "$mode" != preview ]; then
    control=$(sudo -n setpci -s "$bdf" ECAP_ACS+0x6.w)
    [ "$control" = 0000 ] || fail "ACS remains enabled on $bdf: $control"
    echo "ACS_VERIFIED $boot_id $bdf $control"
  fi
done
[ "$(cat "$procfs/sys/kernel/random/boot_id")" = "$boot_id" ] || fail "boot ID changed during ACS operation"
echo "ACS_${mode}_COMPLETE"
