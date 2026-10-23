#!/usr/bin/env bash
# Open Persona: checks before (and after) bringing the stack up on the Hetzner box.
#
#   bin/preflight.sh --env-only   env files present, private, complete (deploy.sh runs this)
#   bin/preflight.sh              also: firewall, DNS, listeners, LiveKit UDP
#
# Prints names and states only, never a value. Exit 1 on any failed check.

set -uo pipefail

OP_ENV_DIR="${OP_ENV_DIR:-/opt/open-persona/env}"
ENV_ONLY=0
[ "${1:-}" = "--env-only" ] && ENV_ONLY=1

fails=0
ok()   { echo "preflight: ok    $*"; }
bad()  { echo "preflight: FAIL  $*" >&2; fails=$((fails + 1)); }
warn() { echo "preflight: warn  $*" >&2; }

# --- env files ------------------------------------------------------------------------
selfhosted=0
grep -q 'livekit\.openpersonasai\.com' "${OP_ENV_DIR}/voice.env" 2>/dev/null && selfhosted=1
required="common api voice postgres caddy"
[ "${selfhosted}" = 1 ] && required="${required} livekit"
for f in ${required}; do
  path="${OP_ENV_DIR}/${f}.env"
  if [ ! -f "${path}" ]; then bad "${f}.env missing"; continue; fi
  mode="$(stat -c '%a' "${path}")"
  [ "${mode}" = "600" ] || bad "${f}.env mode is ${mode}, want 600"
  # Any token left means a value nobody has supplied yet.
  left="$(grep -oE '^[A-Z][A-Z0-9_]*=.*__(OWNER|GITHUB_PRODUCTION|MISSING_FROM_DOTENV|LIVEKIT_[A-Z_]+|GEN_[A-Z0-9_]+|FROM_ENV[A-Z0-9_:]*)__' "${path}" | cut -d= -f1)"
  if [ -n "${left}" ]; then
    bad "${f}.env still has unresolved values: $(echo ${left})"
  else
    ok "${f}.env complete"
  fi
done
grep -q "^PERSONA_INITIATIVE_ENABLED='\?false'\?$" "${OP_ENV_DIR}/api.env" 2>/dev/null \
  && ok "initiative OFF (owner condition)" || warn "PERSONA_INITIATIVE_ENABLED is not false: was the A5-R-1 re-run green and did the owner say so?"
grep -q "^PERSONA_ALLOW_PER_TENANT_MCP='\?0'\?$" "${OP_ENV_DIR}/api.env" 2>/dev/null \
  && ok "per-tenant MCP OFF" || warn "PERSONA_ALLOW_PER_TENANT_MCP is not 0, and no Fly runtime exists to serve it"
if grep -qE '^(FLY_API_TOKEN|PERSONA_MCP_RUNTIME_FLY_APP)=' "${OP_ENV_DIR}"/*.env 2>/dev/null; then
  bad "a Fly MCP runtime setting is present; its Fly app no longer exists"
fi

[ "${ENV_ONLY}" = 1 ] && { [ "${fails}" = 0 ] && exit 0 || exit 1; }

# --- host -----------------------------------------------------------------------------
# Every command's output is captured ONCE and searched from a here-string: with pipefail,
# `cmd | grep -q` can report failure when grep exits early and cmd gets SIGPIPE, which
# would turn a positive check into a false FAIL and a negative check into a silent pass.
has()  { grep -q "$2" <<<"$1"; }
hasE() { grep -qE "$2" <<<"$1"; }

ufw_out="$(sudo ufw status 2>&1 || true)"
if has "${ufw_out}" "Status: active"; then
  ok "ufw active"
  for rule in "22/tcp" "80/tcp" "443/tcp" "7881/tcp" "7882/udp"; do
    if hasE "${ufw_out}" "^${rule}"; then ok "ufw allows ${rule}"; else bad "ufw does not allow ${rule}"; fi
  done
  if hasE "${ufw_out}" '^(5432|7880|8000|8001)'; then
    bad "ufw opens an internal port (5432/7880/8000/8001)"
  fi
else
  bad "ufw is not active"
fi
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

# --- listeners ----------------------------------------------------------------------------
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

[ "${fails}" = 0 ] && { echo "preflight: all checks passed"; exit 0; }
echo "preflight: ${fails} check(s) failed" >&2
exit 1
