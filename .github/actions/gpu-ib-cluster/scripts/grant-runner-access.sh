#!/usr/bin/env bash

# Grants the SSH_IMPORT_IDS identities (lp:/gh:) SSH access to this host (the
# runner), so the workflow caller can log in while the job runs. Keys go in a
# marked block of ~/.ssh/authorized_keys that 99-release.sh removes.
#
# Outputs:
#   stdout: user@address of this host and the imported identities
#   GITHUB_OUTPUT (if set): runner_address
#   Exit code: 0 on success, non-zero if no key could be imported

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

read -r -a import_ids <<<"${SSH_IMPORT_IDS//,/ }"
[[ ${#import_ids[@]} -gt 0 ]] || die "SSH_IMPORT_IDS is empty"

keys=""
for id in "${import_ids[@]}"; do
  case "$id" in
    lp:*) url="https://launchpad.net/~${id#lp:}/+sshkeys" ;;
    gh:*) url="https://github.com/${id#gh:}.keys" ;;
    *) log "Skipping unsupported identity '$id' (use lp: or gh:)"; continue ;;
  esac
  if fetched="$(curl -fsSL --retry 3 "$url")" && [[ -n "$fetched" ]]; then
    while IFS= read -r key; do
      [[ -n "$key" ]] && keys+="$key # $id"$'\n'
    done <<<"$fetched"
    log "Imported keys of $id"
  else
    log "No keys found for $id"
  fi
done
[[ -n "$keys" ]] || die "No SSH keys could be imported from: ${import_ids[*]}"

mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
sed -i "\|^# BEGIN $RUNNER_ACCESS_MARKER\$|,\|^# END $RUNNER_ACCESS_MARKER\$|d" ~/.ssh/authorized_keys
{
  echo "# BEGIN $RUNNER_ACCESS_MARKER"
  printf '%s' "$keys"
  echo "# END $RUNNER_ACCESS_MARKER"
} >> ~/.ssh/authorized_keys

addr="$(whoami)@$(hostname -I | awk '{print $1}')"
log "Runner access: ssh $addr (all addresses: $(hostname -I))"
if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
  echo "::notice title=Runner SSH access::ssh $addr (all addresses: $(hostname -I | xargs))"
fi
gh_output runner_address "$addr"
