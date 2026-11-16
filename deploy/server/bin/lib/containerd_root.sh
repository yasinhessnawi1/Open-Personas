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

# containerd_holds_nothing
# Whether moving containerd's root would orphan nothing (review M1). Two signals, either one
# refuses (status 1, a message on stderr):
#   * containerd's current root (CONTAINERD_DEFAULT_ROOT, /var/lib/containerd: the config sets
#     none, or this would not be asked) holds image content: blobs, or snapshots of any
#     snapshotter. Read from the disk, so it holds when Docker is down;
#   * Docker, when installed, holds containers or images; or it is installed and does not
#     answer, so what it holds cannot be known.
containerd_holds_nothing() {
  local current="${CONTAINERD_DEFAULT_ROOT:-/var/lib/containerd}" d
  for d in "${current}/io.containerd.content.v1.content/blobs/sha256" \
      "${current}"/io.containerd.snapshotter.v1.*/snapshots; do
    if [ -d "${d}" ] && [ -n "$(ls -A "${d}" 2>/dev/null)" ]; then
      echo "containerd's root ${current} holds image content (${d#"${current}/"}); move it by hand first (RUNBOOK.md)" >&2
      return 1
    fi
  done
  if command -v docker >/dev/null 2>&1; then
    if ! docker info >/dev/null 2>&1; then
      echo "Docker is installed but does not answer, so what it holds cannot be known" >&2
      return 1
    fi
    if [ -n "$(docker ps -aq 2>/dev/null)" ] || [ -n "$(docker images -q 2>/dev/null)" ]; then
      echo "Docker holds containers or images; move them before moving containerd's root" >&2
      return 1
    fi
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
# systemd reloads when either changed; containerd restarts only when its config changed (a
# drop-in alone takes effect at the next start, and restarting containerd under a running app
# is not what a re-provision should do).
apply_containerd_root() {
  # Every step ends in `|| return 1` (review M2): the caller runs this in a `||` list, where
  # bash turns errexit off, so a failing step would otherwise be skipped silently.
  local config="$1" root="$2" mount="$3" dropin="$4" wanted unit config_changed=0 dropin_changed=0
  wanted="$(containerd_config_with_root "${config}" "${root}")" || return 1
  if [ "${wanted}" != "$(cat "${config}" 2>/dev/null || true)" ]; then
    containerd_holds_nothing || return 1
    install -d -m 0711 "${root}" || return 1
    install -d -m 0755 "$(dirname "${config}")" || return 1
    # The first rewrite keeps the package's config beside it; a later one never overwrites it.
    if [ -e "${config}" ] && [ ! -e "${config}.bak-r9289" ]; then
      cp -p "${config}" "${config}.bak-r9289" || return 1
    fi
    # Written whole, then moved over the config: containerd never reads half of it.
    printf '%s\n' "${wanted}" > "${config}.tmp" || return 1
    chmod 0644 "${config}.tmp" || return 1
    mv -f "${config}.tmp" "${config}" || return 1
    config_changed=1
  fi
  if [ -n "${mount}" ]; then
    unit="$(printf '[Unit]\nRequiresMountsFor=%s' "${mount}")"
    if [ "${unit}" != "$(cat "${dropin}" 2>/dev/null || true)" ]; then
      install -d -m 0755 "$(dirname "${dropin}")" || return 1
      printf '%s\n' "${unit}" > "${dropin}.tmp" || return 1
      chmod 0644 "${dropin}.tmp" || return 1
      mv -f "${dropin}.tmp" "${dropin}" || return 1
      dropin_changed=1
    fi
  fi
  if [ "${config_changed}" = 1 ] || [ "${dropin_changed}" = 1 ]; then
    systemctl daemon-reload || return 1
  fi
  if [ "${config_changed}" = 1 ]; then
    systemctl restart containerd || return 1
  fi
}
