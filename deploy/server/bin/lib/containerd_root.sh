# shellcheck shell=bash
# Open Persona: containerd's root on the block volume (R9-289). Sourced by provision.sh,
# never run.
#
# Docker stores image layers through containerd's snapshotter, under containerd's own root
# (/var/lib/containerd), not under Docker's data-root. On the core host that root is on the
# boot disk, which the images filled. containerd reads `root` from the top of
# /etc/containerd/config.toml: a top-level key, so it must come before the first table.

# containerd_config_with_root <config.toml> <root>
# Prints the config with `root = "<root>"` as its first line. Prints it unchanged when its
# top level already sets that root (idempotent). Refuses (status 1, a message on stderr)
# when its top level sets another root: moving it would orphan what is stored there. A
# missing file is an empty config.
containerd_config_with_root() {
  local config="$1" root="$2" current
  # Never fails, so a caller under `set -e` is not stopped by a missing file.
  current="$(
    if [ -r "${config}" ]; then
      awk '
        /^[[:space:]]*\[/ { exit }
        /^[[:space:]]*root[[:space:]]*=/ {
          sub(/^[[:space:]]*root[[:space:]]*=[[:space:]]*/, "")
          gsub(/^"|"[[:space:]]*(#.*)?$/, "")
          print
          exit
        }
      ' "${config}"
    fi
  )"
  if [ -n "${current}" ] && [ "${current}" != "${root}" ]; then
    echo "containerd's config already sets root to ${current}, not ${root}" >&2
    return 1
  fi
  if [ -z "${current}" ]; then
    printf 'root = "%s"\n' "${root}"
  fi
  if [ -r "${config}" ]; then
    cat "${config}"
  fi
}

# apply_containerd_root <config.toml> <root> <data mount, or ""> <drop-in file>
# Puts containerd's root at <root>, decided first and changed second:
#   * the config already sets <root> (and the drop-in is in place): nothing happens at all,
#     not even a restart, whatever Docker holds. A host moved by hand, or provisioned
#     before, re-runs as a clean no-op;
#   * the config must change: refused (status 1) while Docker holds containers or images,
#     which the move would orphan; otherwise the config is rewritten;
#   * the drop-in makes containerd wait for the block volume, as docker.service does.
# containerd restarts only when something changed.
apply_containerd_root() {
  local config="$1" root="$2" mount="$3" dropin="$4" wanted unit changed=0
  wanted="$(containerd_config_with_root "${config}" "${root}")" || return 1
  if [ "${wanted}" != "$(cat "${config}" 2>/dev/null || true)" ]; then
    if [ -n "$(docker ps -aq 2>/dev/null)$(docker images -q 2>/dev/null)" ]; then
      echo "Docker holds containers or images; move them before putting containerd's root at ${root}" >&2
      return 1
    fi
    install -d -m 0711 "${root}"
    install -d -m 0755 "$(dirname "${config}")"
    printf '%s\n' "${wanted}" > "${config}"
    chmod 0644 "${config}"
    changed=1
  fi
  if [ -n "${mount}" ]; then
    unit="$(printf '[Unit]\nRequiresMountsFor=%s' "${mount}")"
    if [ "${unit}" != "$(cat "${dropin}" 2>/dev/null || true)" ]; then
      install -d -m 0755 "$(dirname "${dropin}")"
      printf '%s\n' "${unit}" > "${dropin}"
      chmod 0644 "${dropin}"
      changed=1
    fi
  fi
  if [ "${changed}" = 1 ]; then
    systemctl daemon-reload
    systemctl restart containerd
  fi
}
