#!/usr/bin/env bash
# Open Persona: fill the values that are generated ON THE BOX into the rendered env files.
#
# render_env.py (owner's PC) leaves `__GEN_<NAME>__` tokens for everything that must be
# new on this box: the two database passwords and the LiveKit key pair, plus the box's
# public IPv4 for LiveKit's ICE candidate. This script generates each value ONCE, keeps it
# in ${OP_ENV_DIR}/.generated (mode 0600), and substitutes it in every env file. Running it
# again is safe: a value already generated is reused, never replaced, so the database
# password in postgres.env and in the DSNs of api.env and voice.env always agree.
#
# It prints names only, never values.
#
# Usage: bin/fill_generated.sh            (OP_ENV_DIR defaults to /opt/open-persona/env)

set -euo pipefail
umask 077

OP_ENV_DIR="${OP_ENV_DIR:-/opt/open-persona/env}"
STORE="${OP_ENV_DIR}/.generated"

die() { echo "fill_generated: FAILED: $*" >&2; exit 1; }
[ -d "${OP_ENV_DIR}" ] || die "no ${OP_ENV_DIR}"
touch "${STORE}"
chmod 600 "${STORE}"

hex() { openssl rand -hex "$1"; }

public_ipv4() {
  # The source address the kernel uses for the internet. On a Hetzner Cloud server the
  # primary IPv4 sits on the interface itself (no NAT), so this is the public address.
  local ip
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
  printf '%s' "${ip}" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || return 1
  case "${ip}" in
    10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*) return 1 ;;
  esac
  printf '%s' "${ip}"
}

generate() {
  case "$1" in
    POSTGRES_PASSWORD|APP_DB_PASSWORD|PLATFORM_DB_PASSWORD) hex 32 ;;
    LIVEKIT_API_KEY) printf 'API%s' "$(hex 8)" ;;
    LIVEKIT_API_SECRET) hex 32 ;;
    NODE_IP) public_ipv4 || die "could not determine the public IPv4; set it by hand in ${STORE} as NODE_IP=<ip>" ;;
    *) die "unknown generated value $1" ;;
  esac
}

stored() { sed -n "s/^$1=//p" "${STORE}" | head -n1; }

tokens="$(cat "${OP_ENV_DIR}"/*.env 2>/dev/null | grep -oE '__GEN_[A-Z0-9_]+__' | sort -u || true)"
if [ -z "${tokens}" ]; then
  echo "fill_generated: nothing to fill"
  exit 0
fi

for token in ${tokens}; do
  name="${token#__GEN_}"; name="${name%__}"
  value="$(stored "${name}")"
  if [ -z "${value}" ]; then
    value="$(generate "${name}")"
    printf '%s=%s\n' "${name}" "${value}" >> "${STORE}"
    echo "fill_generated: generated ${name}"
  else
    echo "fill_generated: reused ${name}"
  fi
  # Generated values are hex, `API`+hex, or a dotted IPv4: none needs sed escaping.
  for f in "${OP_ENV_DIR}"/*.env; do
    if grep -q "${token}" "$f"; then
      sed -i "s|${token}|${value}|g" "$f"
      echo "fill_generated:   into $(basename "$f")"
    fi
  done
done
chmod 600 "${OP_ENV_DIR}"/*.env
echo "fill_generated: done. ${STORE} now holds the generated values: back it up with the env files."
