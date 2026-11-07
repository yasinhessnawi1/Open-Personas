# shellcheck shell=bash
# Open Persona: the ONE way the kit's scripts reach docker compose. Sourced, never run.
#
# It applies the host profile, deploy/server/hosts/<profile>.env, with `--env-file`, so a
# restore, a backup, a preflight and a deploy all start a service with the same limits and
# Postgres settings. The profile is, in order: $OP_HOST_PROFILE, the name provision.sh
# wrote to /etc/open-persona/host-profile, else `oracle` (the only core host today).
#
# The caller sets KIT_DIR (the deploy/server directory) before sourcing this file.

: "${KIT_DIR:?lib/compose.sh needs KIT_DIR}"

if [ -z "${OP_HOST_PROFILE:-}" ] && [ -r /etc/open-persona/host-profile ]; then
  OP_HOST_PROFILE="$(tr -d '[:space:]' < /etc/open-persona/host-profile)"
fi
OP_HOST_PROFILE="${OP_HOST_PROFILE:-oracle}"
# A profile name is a file name under hosts/: nothing that could climb out of it.
if ! printf '%s' "${OP_HOST_PROFILE}" | grep -Eq '^[a-z0-9-]+$' \
    || [ ! -f "${KIT_DIR}/hosts/${OP_HOST_PROFILE}.env" ]; then
  echo "compose: unknown host profile '${OP_HOST_PROFILE}' (see deploy/server/hosts/)" >&2
  exit 1
fi
export OP_HOST_PROFILE

compose() {
  docker compose --env-file "${KIT_DIR}/hosts/${OP_HOST_PROFILE}.env" \
    -f "${KIT_DIR}/docker-compose.yml" "$@"
}
