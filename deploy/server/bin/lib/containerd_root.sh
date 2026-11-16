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
