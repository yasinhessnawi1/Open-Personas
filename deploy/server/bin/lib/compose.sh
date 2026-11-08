# shellcheck shell=bash
# Open Persona: the ONE way the kit's scripts reach docker compose. Sourced, never run.
#
# It loads the host profile (lib/profile.sh: $OP_HOST_PROFILE, else
# /etc/open-persona/host-profile, else oracle; the name must match ^[a-z0-9-]+$) and
# hands the same file to compose with `--env-file`, so a restore, a backup, a preflight
# and a deploy all start a service with the same limits and Postgres settings.
#
# The caller sets KIT_DIR (the deploy/server directory) before sourcing this file.

: "${KIT_DIR:?lib/compose.sh needs KIT_DIR}"
# shellcheck source=profile.sh
. "${KIT_DIR}/bin/lib/profile.sh"

compose() {
  docker compose --env-file "${KIT_DIR}/hosts/${OP_HOST_PROFILE}.env" \
    -f "${KIT_DIR}/docker-compose.yml" "$@"
}
