#!/usr/bin/env bash
# Open Persona: build and roll out the checked-out commit on the Hetzner box.
#
# Order, each step stopping the deploy on failure:
#   1. preflight (env files complete, no token left)
#   2. refuse an EMPTY database: production is restored from backup first (restore.sh);
#      migrating an empty database would create a fresh schema that the restore then
#      refuses to overwrite
#   3. keep the images the running containers use as :previous (the rollback)
#   4. build natively on the box (it competes with live calls: deploy off-hours)
#   5. a pg_dump BEFORE migrating (migrations are not reversible by image rollback)
#   6. migrate: the `migrate` service runs packages/api/release.py once
#   7. up -d, then wait for api /livez and /healthz and the voice listener
#   8. smoke through Caddy
#
# The LiveKit mode is read from voice.env, so there is one source of truth: a
# PERSONA_VOICE_LIVEKIT_URL on livekit.openpersonasai.com means selfhosted (the
# `selfhosted-livekit` profile runs); anything else means LiveKit Cloud.
#
# Usage: bin/deploy.sh [--skip-build]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_DIR="$(dirname "${SCRIPT_DIR}")"
export OP_ENV_DIR="${OP_ENV_DIR:-/opt/open-persona/env}"
export OP_DATA_DIR="${OP_DATA_DIR:-/srv/open-persona}"
export OP_IMAGE_TAG="${OP_IMAGE_TAG:-current}"
SKIP_BUILD=0
[ "${1:-}" = "--skip-build" ] && SKIP_BUILD=1

die() { echo "deploy: FAILED: $*" >&2; exit 1; }
say() { echo "deploy: $*" >&2; }

if grep -q 'livekit\.openpersonasai\.com' "${OP_ENV_DIR}/voice.env" 2>/dev/null; then
  LIVEKIT_MODE=selfhosted
  export COMPOSE_PROFILES=selfhosted-livekit
else
  LIVEKIT_MODE=cloud
fi
compose() { docker compose -f "${KIT_DIR}/docker-compose.yml" "$@"; }

# --- 1 ------------------------------------------------------------------------------
"${SCRIPT_DIR}/preflight.sh" --env-only || die "preflight"
say "LiveKit mode: ${LIVEKIT_MODE}"

# --- 2 ------------------------------------------------------------------------------
compose up -d postgres
ready=0
for _ in $(seq 1 60); do
  if compose exec -T postgres pg_isready -h 127.0.0.1 -U persona -d persona >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 2
done
[ "${ready}" = 1 ] || die "postgres did not become ready"
tables="$(compose exec -T postgres psql -U persona -d persona -X -q -t -A \
  -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")" || die "could not query the database"
[ "${tables}" != "0" ] || die "the database is empty: run bin/restore.sh first (RUNBOOK.md step 5)"
# The api's DATABASE_URL is persona_platform: it must exist and must not be a superuser.
platform_super="$(compose exec -T postgres psql -U persona -d persona -X -q -t -A \
  -c "SELECT rolsuper::text FROM pg_roles WHERE rolname = 'persona_platform'")" \
  || die "could not query the database"
[ "${platform_super}" = "false" ] \
  || die "persona_platform is missing or a superuser: run bin/restore.sh --roles-only"

# --- 3 ------------------------------------------------------------------------------
# :previous is the image the RUNNING container uses, not whatever :current points at:
# after a failed migrate, :current is already the new build while the old containers
# still serve. Only a build replaces :current, so --skip-build leaves :previous alone.
if [ "${SKIP_BUILD}" = 0 ]; then
  for svc in api voice livekit; do
    cid="$(compose ps -q "${svc}" 2>/dev/null || true)"
    [ -n "${cid}" ] || continue
    running="$(docker inspect --format '{{.Image}}' "${cid}")"
    docker image tag "${running}" "open-persona/${svc}:previous"
    say "rollback image for ${svc}: ${running:7:12}"
  done
fi

# --- 4 ------------------------------------------------------------------------------
# The build runs inside dockerd (BuildKit), so `nice` on this client would change
# nothing: a build competes with live calls for CPU. Deploy outside call hours.
if [ "${SKIP_BUILD}" = 0 ]; then
  say "building $(git -C "${KIT_DIR}" rev-parse --short HEAD)"
  compose build
fi

# --- 5 ------------------------------------------------------------------------------
umask 077
mkdir -p "${OP_DATA_DIR}/backups"
dump="${OP_DATA_DIR}/backups/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ).dump"
say "pre-migrate dump: ${dump}"
compose exec -T postgres pg_dump -U persona -Fc persona > "${dump}" || die "pre-migrate dump"
chmod 600 "${dump}"

# --- 6 ------------------------------------------------------------------------------
say "migrating"
compose run --rm migrate || die "migration failed; nothing was restarted (the old containers still serve)"

# --- 7 ------------------------------------------------------------------------------
# Caddy runs with its admin API off, so a bad Caddyfile is only found at start: check it
# first, with the same image and env file.
compose run --rm --no-deps -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
  >/dev/null || die "the Caddyfile does not validate (run the same command without >/dev/null)"
say "starting"
compose up -d --remove-orphans

wait_for() {
  local what="$1" seconds="$2"; shift 2
  for _ in $(seq 1 $((seconds / 5))); do
    if "$@" >/dev/null 2>&1; then say "${what}: ok"; return 0; fi
    sleep 5
  done
  return 1
}
wait_for "api /livez" 900 curl -fsS --max-time 4 http://127.0.0.1:8000/livez || die "api /livez"
wait_for "api /healthz (database)" 120 curl -fsS --max-time 4 http://127.0.0.1:8000/healthz || die "api /healthz"
wait_for "voice listener" 600 curl -s -o /dev/null --max-time 4 http://127.0.0.1:8001/v1/voices || die "voice listener"

# --- 8 ------------------------------------------------------------------------------
insecure=""
grep -q 'acme-staging' "${OP_ENV_DIR}/caddy.env" && insecure="-k"
if wait_for "caddy -> api" 120 curl -fsS ${insecure} --max-time 6 \
    --resolve api.openpersonasai.com:443:127.0.0.1 https://api.openpersonasai.com/livez; then
  :
else
  say "WARNING: Caddy did not serve api.openpersonasai.com yet (DNS or the certificate). The services are up."
fi

say "deployed $(git -C "${KIT_DIR}" rev-parse --short HEAD). Rollback: RUNBOOK.md, section 'Rolling back'."
