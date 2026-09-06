#!/usr/bin/env bash
# Copy the captured connector secrets onto the api app (Spec I1, the connectors fold).
#
# The fold hosts the connector transports inside the api process, so the api needs the
# same PERSONA_CONNECTORS_* credentials the standalone connectors app holds. Fly secrets
# are write-only, so the source is the capture taken while the connectors machine was
# still running (scripts/migration/capture-fly-secrets.sh), NOT `flyctl secrets list`.
#
# Names are copied UNCHANGED. ConnectorConfig reads the PERSONA_CONNECTORS_ prefix
# directly, so renaming anything here silently unconfigures that platform: a Pydantic
# Settings model with extra="ignore" drops an unrecognised name without complaint
# (Spec I1 D-I1-19).
#
# Usage:
#   ./set-connector-secrets-on-api.sh ~/secure/persona-fly-secrets/open-persona-connectors.env
#   ./set-connector-secrets-on-api.sh <file> --apply     # actually writes
#
# WITHOUT --apply this prints exactly what it would do and writes nothing. That is the
# default on purpose: setting secrets RESTARTS the api, and during the cutover the order
# of restarts is what keeps two consumers off one Slack socket. Read the runbook in
# docs/ops/ before using --apply.
set -euo pipefail

SRC="${1:?usage: set-connector-secrets-on-api.sh <captured-connectors.env> [--apply]}"
APPLY="${2:-}"
APP="${PERSONA_API_FLY_APP:-open-persona-api}"

[ -f "$SRC" ] || { echo "no such file: $SRC" >&2; exit 1; }

# Only the connector-owned names travel. The api has its own DATABASE_URL,
# APP_DATABASE_URL, JWT keys and model keys, and copying the connectors app's versions
# over them is how a fold breaks the surface it was meant to preserve.
#
# The two DSNs below are PERSONA_CONNECTORS_-prefixed and would otherwise ride along, so
# they are excluded BY NAME. Embedded, the connectors run on the engines the api already
# built (Spec I1 D-I1-18): the api's APP_DATABASE_URL is the owner-scoped persona_app
# engine, and its DATABASE_URL is the cross-tenant dispatch engine. Setting the
# connectors' own copies here would put a second, independently-drifting definition of
# the same two roles on the app, which is the R9-123 failure waiting to happen again:
# there, an owner-scoped engine ran on a role Postgres exempts from RLS, every owner
# scope was honoured by nothing, and one linked user was offered fifteen personas across
# five accounts. They are inert today, which is exactly why a wrong value would sit
# unnoticed until something started reading them.
EXCLUDED=(PERSONA_CONNECTORS_DATABASE_URL PERSONA_CONNECTORS_APP_DATABASE_URL)

is_excluded() {
  local candidate="$1" name
  for name in "${EXCLUDED[@]}"; do
    [ "$candidate" = "$name" ] && return 0
  done
  return 1
}

# `while read` rather than `mapfile`: macOS ships bash 3.2 at /bin/bash and mapfile is a
# bash 4 builtin, so the mapfile version died with "command not found" on the machine this
# is actually run from. The sibling capture-fly-secrets.sh reads the same way.
CANDIDATES=0
PAIRS=()
SKIPPED=()
while IFS= read -r pair; do
  [ -z "$pair" ] && continue
  CANDIDATES=$((CANDIDATES + 1))
  if is_excluded "${pair%%=*}"; then
    SKIPPED+=("${pair%%=*}")
  else
    PAIRS+=("$pair")
  fi
done < <(grep -E '^PERSONA_CONNECTORS_[A-Z0-9_]+=' "$SRC" || true)

if [ "${#PAIRS[@]}" -eq 0 ]; then
  echo "no copyable PERSONA_CONNECTORS_* entries in $SRC — refusing to run" >&2
  exit 1
fi

echo "app:    $APP"
echo "source: $SRC"
echo "found:  $CANDIDATES PERSONA_CONNECTORS_* secrets"
echo "copy:   ${#PAIRS[@]}"
echo "skip:   ${#SKIPPED[@]}"
echo
for pair in "${PAIRS[@]}"; do
  # Never print a value. The name and its length are enough to audit the copy.
  name="${pair%%=*}"
  value="${pair#*=}"
  printf '  COPY %-51s (%d chars)\n' "$name" "${#value}"
done
if [ "${#SKIPPED[@]}" -gt 0 ]; then
  for name in "${SKIPPED[@]}"; do
    printf '  SKIP %-51s (the api supplies this role itself; D-I1-18)\n' "$name"
  done
fi
echo

if [ "$APPLY" != "--apply" ]; then
  cat <<'DRY'
DRY RUN. Nothing was written.

Re-run with --apply ONLY at the point the runbook says to, and remember that
`flyctl secrets set` restarts the app. In the cutover that restart is step 1's
flag-OFF deploy, not the flag flip.
DRY
  exit 0
fi

# One call: flyctl restarts the app once per invocation, so batching keeps the
# restart count at one instead of 55.
flyctl secrets set -a "$APP" "${PAIRS[@]}"

echo
echo "Set ${#PAIRS[@]} secrets on $APP. Verify names (values are never readable back):"
echo "  flyctl secrets list -a $APP | grep PERSONA_CONNECTORS_ | wc -l"
