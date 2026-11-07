# shellcheck shell=bash
# Open Persona: load the host profile into the calling script. Sourced, never run.
#
# The profile is deploy/server/hosts/<name>.env: plain, non-secret OP_* settings (sizes,
# the data uid, the block volume, the SSH allow list). The name is, in order:
# $OP_HOST_PROFILE, what provision.sh wrote to /etc/open-persona/host-profile, else
# `oracle` (the only core host today).
#
# Every setting is exported unless the caller already set it, so an operator can override
# one value for one run (`OP_BACKUP_MIN_FREE_GB=5 bin/backup.sh`). A line that is not a
# plain `OP_NAME=value` (letters, digits and . _ , / : - only) stops the script: the file
# is sourced by root, so nothing in it may expand, quote or chain a command.
#
# The caller sets KIT_DIR (the deploy/server directory) before sourcing this file.

: "${KIT_DIR:?lib/profile.sh needs KIT_DIR}"

if [ -z "${OP_HOST_PROFILE:-}" ] && [ -r /etc/open-persona/host-profile ]; then
  OP_HOST_PROFILE="$(tr -d '[:space:]' < /etc/open-persona/host-profile)"
fi
OP_HOST_PROFILE="${OP_HOST_PROFILE:-oracle}"
# A profile name is a file name under hosts/: nothing that could climb out of it.
if ! printf '%s' "${OP_HOST_PROFILE}" | grep -Eq '^[a-z0-9-]+$' \
    || [ ! -f "${KIT_DIR}/hosts/${OP_HOST_PROFILE}.env" ]; then
  echo "profile: unknown host profile '${OP_HOST_PROFILE}' (see deploy/server/hosts/)" >&2
  exit 1
fi
export OP_HOST_PROFILE
OP_PROFILE_FILE="${KIT_DIR}/hosts/${OP_HOST_PROFILE}.env"

while IFS= read -r _op_line || [ -n "${_op_line}" ]; do
  case "${_op_line}" in '' | '#'*) continue ;; esac
  if ! printf '%s' "${_op_line}" | grep -Eq '^OP_[A-Z0-9_]+=[A-Za-z0-9._,/:-]*$'; then
    echo "profile: hosts/${OP_HOST_PROFILE}.env: not a plain OP_* setting: ${_op_line%%=*}" >&2
    exit 1
  fi
  _op_name="${_op_line%%=*}"
  if [ -z "${!_op_name+x}" ]; then
    export "${_op_name}=${_op_line#*=}"
  fi
done < "${OP_PROFILE_FILE}"
unset _op_line _op_name

# Refuses to go on when the profile names a data mount (the block volume) that is not a
# mounted filesystem: production data must never land on the boot disk because the
# volume failed to attach or mount. A profile without OP_DATA_MOUNT has no such volume.
require_data_mount() {
  [ -n "${OP_DATA_MOUNT:-}" ] || return 0
  if ! mountpoint -q "${OP_DATA_MOUNT}"; then
    echo "profile: ${OP_DATA_MOUNT} is not a mounted filesystem: the block volume is missing" \
      "(RUNBOOK.md, 'The block volume'); refusing to write data to the boot disk" >&2
    exit 1
  fi
}
