#!/usr/bin/env bash
# Open Persona: checks before (and after) bringing the stack up on the Hetzner box.
#
#   bin/preflight.sh --env-only   env files present, private, complete, roles separated
#                                 (bin/deploy.sh runs this)
#   bin/preflight.sh              also: alerts, database roles, firewall and listener
#                                 allowlists, published ports, DNS
#
# Prints names and states only, never a value. Exit 1 on any failed check.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_DIR="$(dirname "${SCRIPT_DIR}")"
OP_ENV_DIR="${OP_ENV_DIR:-/opt/open-persona/env}"
OP_ALERT_DIR="${OP_ALERT_DIR:-/var/lib/open-persona/alerts}"
ENV_ONLY=0
[ "${1:-}" = "--env-only" ] && ENV_ONLY=1

fails=0
ok()   { echo "preflight: ok    $*"; }
bad()  { echo "preflight: FAIL  $*" >&2; fails=$((fails + 1)); }
warn() { echo "preflight: warn  $*" >&2; }
# Output is captured once and searched from a here-string: with pipefail,
# `cmd | grep -q` can report failure when grep exits early and cmd gets SIGPIPE.
has()  { grep -q "$2" <<<"$1"; }
hasE() { grep -qE "$2" <<<"$1"; }
dsn_user() { sed -n "s|^$2='\{0,1\}postgresql+psycopg://\([a-z_]*\):.*|\1|p" "${OP_ENV_DIR}/$1" 2>/dev/null; }

# --- env files ------------------------------------------------------------------------
selfhosted=0
grep -q 'livekit\.openpersonasai\.com' "${OP_ENV_DIR}/voice.env" 2>/dev/null && selfhosted=1
required="common api voice postgres migrate caddy alert"
[ "${selfhosted}" = 1 ] && required="${required} livekit"
for f in ${required}; do
  path="${OP_ENV_DIR}/${f}.env"
  if [ ! -f "${path}" ]; then bad "${f}.env missing"; continue; fi
  mode="$(stat -c '%a' "${path}")"
  [ "${mode}" = "600" ] || bad "${f}.env mode is ${mode}, want 600"
  # Any token left means a value nobody has supplied yet.
  left="$(grep -oE '^[A-Z][A-Z0-9_]*=.*__(OWNER|OWNER_CLERK_PROD|GITHUB_PRODUCTION|MISSING_FROM_DOTENV|LIVEKIT_[A-Z_]+|GEN_[A-Z0-9_]+|FROM_ENV[A-Z0-9_:]*)__' "${path}" | cut -d= -f1)"
  if [ -n "${left}" ]; then
    bad "${f}.env still has unresolved values: $(echo ${left})"
  else
    ok "${f}.env complete"
  fi
done

# Database roles, by DSN: the superuser only in migrate.env (the one-shot migration),
# the api's cross-tenant engines on persona_platform, every request path on persona_app.
[ "$(dsn_user api.env DATABASE_URL)" = "persona_platform" ] \
  && ok "api DATABASE_URL is persona_platform" || bad "api.env DATABASE_URL must use persona_platform"
[ "$(dsn_user api.env APP_DATABASE_URL)" = "persona_app" ] \
  && ok "api APP_DATABASE_URL is persona_app" || bad "api.env APP_DATABASE_URL must use persona_app"
[ "$(dsn_user voice.env PERSONA_VOICE_DATABASE_URL)" = "persona_app" ] \
  && ok "voice DSN is persona_app" || bad "voice.env PERSONA_VOICE_DATABASE_URL must use persona_app"
[ "$(dsn_user migrate.env DATABASE_URL)" = "persona" ] \
  && ok "migrate.env holds the superuser DSN" || bad "migrate.env DATABASE_URL must use the superuser persona"
superuser_files="$(grep -l 'postgresql+psycopg://persona:' "${OP_ENV_DIR}"/*.env 2>/dev/null | xargs -r -n1 basename | grep -vx migrate.env)"
[ -z "${superuser_files}" ] || bad "the superuser DSN appears outside migrate.env: $(echo ${superuser_files})"

# A development Clerk instance in production would let anyone with a dev account sign in.
if grep -l 'clerk\.accounts\.dev' "${OP_ENV_DIR}"/*.env >/dev/null 2>&1; then
  bad "an env file names a *.clerk.accounts.dev (development) Clerk instance"
else
  ok "no development Clerk instance named"
fi

grep -q "^PERSONA_INITIATIVE_ENABLED='\?false'\?$" "${OP_ENV_DIR}/api.env" 2>/dev/null \
  && ok "initiative OFF (owner condition)" || warn "PERSONA_INITIATIVE_ENABLED is not false: was the A5-R-1 re-run green and did the owner say so?"
grep -q "^PERSONA_ALLOW_PER_TENANT_MCP='\?0'\?$" "${OP_ENV_DIR}/api.env" 2>/dev/null \
  && ok "per-tenant MCP OFF" || warn "PERSONA_ALLOW_PER_TENANT_MCP is not 0: is its runtime (the MCP seam) deployed?"
if grep -qE '^(FLY_API_TOKEN|PERSONA_MCP_RUNTIME_FLY_APP)=' "${OP_ENV_DIR}"/*.env 2>/dev/null; then
  bad "a Fly MCP runtime setting is present; its Fly app no longer exists"
fi

[ "${ENV_ONLY}" = 1 ] && { [ "${fails}" = 0 ] && exit 0 || exit 1; }

# --- alerts ----------------------------------------------------------------------------
if [ -s "${OP_ALERT_DIR}/ALERTS" ]; then
  bad "$(wc -l < "${OP_ALERT_DIR}/ALERTS") unacknowledged alert(s) in ${OP_ALERT_DIR}/ALERTS (bin/alert.sh --ack once handled)"
else
  ok "no unacknowledged alerts"
fi

# --- database roles, live -----------------------------------------------------------------
roles="$(docker compose -f "${KIT_DIR}/docker-compose.yml" exec -T postgres psql -U persona -d persona -X -q -t -A -F '|' \
  -c "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname IN ('persona_app', 'persona_platform')" 2>/dev/null)"
has "${roles}" "persona_app|f|f" && ok "persona_app: not superuser, RLS enforced" || bad "persona_app is missing, a superuser, or bypasses RLS"
has "${roles}" "persona_platform|f|t" && ok "persona_platform: not superuser" || bad "persona_platform is missing or a superuser"

# --- firewall: exactly the allowed rules ---------------------------------------------------
ALLOWED_UFW="22/tcp 80/tcp 443/tcp 7881/tcp 7882/udp"
ufw_out="$(sudo ufw status 2>&1 || true)"
if has "${ufw_out}" "Status: active"; then
  ok "ufw active"
  for rule in ${ALLOWED_UFW}; do
    if hasE "${ufw_out}" "^${rule} "; then ok "ufw allows ${rule}"; else bad "ufw does not allow ${rule}"; fi
  done
  # Every rule line starts with its port/proto; anything not on the list is a finding.
  extra="$(awk 'NR>4 && NF {print $1}' <<<"${ufw_out}" | sort -u | grep -vxE "$(tr ' ' '|' <<<"${ALLOWED_UFW}")" || true)"
  [ -z "${extra}" ] && ok "ufw opens nothing else" || bad "ufw opens rules outside the allowlist: $(echo ${extra})"
else
  bad "ufw is not active"
fi

# --- listeners: nothing public that is not on the list ------------------------------------
# Public (non-loopback) listeners allowed: ssh, caddy (80, 443 tcp; 443 udp is HTTP/3, which
# ufw does not open), livekit (7880 behind ufw for Caddy, 7881, 7882) and DHCP clients.
ALLOWED_TCP="22 80 443 7880 7881"
ALLOWED_UDP="68 443 546 7882"
public_ports() {  # public_ports <ss flags>
  ss -H "$1" | awk '{print $4}' \
    | grep -vE '^(127\.[0-9.]+(%lo)?|\[::1\](%lo)?|\[::ffff:127\.[0-9.]+\]):' \
    | sed -E 's/.*:([0-9]+)$/\1/' | sort -un
}
for pair in "tcp:-ltn:${ALLOWED_TCP}" "udp:-lun:${ALLOWED_UDP}"; do
  proto="${pair%%:*}"; rest="${pair#*:}"; flags="${rest%%:*}"; allowed="${rest#*:}"
  unexpected=""
  for port in $(public_ports "${flags}"); do
    grep -qw "${port}" <<<"${allowed}" || unexpected="${unexpected} ${port}"
  done
  [ -z "${unexpected}" ] && ok "public ${proto} listeners all on the allowlist" \
    || bad "public ${proto} listener(s) outside the allowlist:${unexpected}"
done
tcp_out="$(ss -ltn)"
udp_out="$(ss -lun)"
listening() {  # listening <output> <pattern> <label>
  if hasE "$1" "$2"; then ok "$3"; else bad "$3: not listening"; fi
}
listening "${tcp_out}" '127\.0\.0\.1:8000 ' "api on 127.0.0.1:8000"
listening "${tcp_out}" '127\.0\.0\.1:8001 ' "voice on 127.0.0.1:8001"
listening "${tcp_out}" ':443 ' "caddy on :443"
if [ "${selfhosted}" = 1 ]; then
  listening "${tcp_out}" ':7880 ' "livekit signaling :7880"
  listening "${tcp_out}" ':7881 ' "livekit ICE/TCP :7881"
  listening "${udp_out}" ':7882 ' "livekit ICE/UDP :7882"
  warn "a listening UDP socket does not prove media flows: run the browser check in RUNBOOK.md step 9"
fi

# --- published ports -----------------------------------------------------------------------
ports_out="$(docker ps --format '{{.Ports}}' 2>&1)" || bad "docker ps failed"
if hasE "${ports_out}" '0\.0\.0\.0:|\[::\]:'; then
  bad "a container publishes on a public address (Docker bypasses ufw)"
else
  ok "no container publishes on a public address"
fi

# --- DNS points here --------------------------------------------------------------------
ip4="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}' || true)"
for host in api.openpersonasai.com api.openpersona.online connectors.openpersonasai.com \
            voice.openpersonasai.com livekit.openpersonasai.com; do
  resolved="$(getent ahostsv4 "${host}" | awk 'NR==1 {print $1}' || true)"
  if [ "${resolved}" = "${ip4}" ]; then ok "${host} -> this box"
  else warn "${host} resolves to '${resolved:-nothing}', not this box (${ip4})"; fi
done

[ "${fails}" = 0 ] && { echo "preflight: all checks passed"; exit 0; }
echo "preflight: ${fails} check(s) failed" >&2
exit 1
