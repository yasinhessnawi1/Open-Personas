#!/usr/bin/env bash
# Open Persona: restore production from the Fly backup set onto the Hetzner box.
#
# Restore-from-backup, not a live migration: the Fly apps were deleted on 2026-10-02 and
# the backup set in ~/Backups/fly-2026-10-02 is the only copy of production. This script
# never modifies the backup files and never prints their contents, only counts and
# schema facts.
#
# What it does, in order (RUNBOOK.md step 5):
#   1. Verifies the backup checksums (SHA256SUMS written on the owner's PC).
#   2. Checks the target database is EMPTY. It refuses to restore over data.
#   3. Recreates persona_app with the attributes globals.sql records (checked, not
#      replayed: the old password hashes are never applied) and a NEW password.
#      The superuser role `persona` is created by the postgres image itself from
#      POSTGRES_USER / POSTGRES_PASSWORD, again with a new password.
#   4. Installs pgvector BEFORE the data (the dump's CREATE EXTENSION needs it).
#   5. pg_restore in one transaction, stopping at the first error.
#   6. Runs sql/verify.sql and compares the facts with this backup set's known shape.
#   7. Unpacks the api volume tarball into the api's data directory (skipped with
#      --db-only).
# Migrations to the current head are NOT run here. bin/deploy.sh runs them through the
# `migrate` service, so the restored data meets the code exactly the way every deploy
# does.
#
# Usage on the box (from the checkout's deploy/hetzner directory):
#   bin/restore.sh --backup-dir /srv/open-persona/restore
# Rehearsal against a throwaway container (no compose, no volume):
#   bin/restore.sh --backup-dir DIR --container NAME --app-password-file FILE --db-only
#
# Exit codes: 0 restored and verified, 1 a precondition or a check failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_DIR="$(dirname "${SCRIPT_DIR}")"

# This backup set's known shape, read from its TOC on 2026-10-23. Override only for a
# different backup set, and say why in the runbook log.
EXPECT_TABLES="${EXPECT_TABLES:-54}"
EXPECT_FORCED_RLS="${EXPECT_FORCED_RLS:-45}"
EXPECT_POLICIES="${EXPECT_POLICIES:-46}"

BACKUP_DIR=""
CONTAINER=""
APP_PASSWORD_FILE=""
DB_ONLY=0
OP_ENV_DIR="${OP_ENV_DIR:-/opt/open-persona/env}"
OP_DATA_DIR="${OP_DATA_DIR:-/srv/open-persona}"

die() { echo "restore: FAILED: $*" >&2; exit 1; }
say() { echo "restore: $*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
    --container) CONTAINER="$2"; shift 2 ;;
    --app-password-file) APP_PASSWORD_FILE="$2"; shift 2 ;;
    --db-only) DB_ONLY=1; shift ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "${BACKUP_DIR}" ] || die "--backup-dir is required"
DUMP="${BACKUP_DIR}/persona.dump"
GLOBALS="${BACKUP_DIR}/globals.sql"
VOLUME="${BACKUP_DIR}/persona_volume.tgz"
for f in "${DUMP}" "${GLOBALS}"; do [ -f "$f" ] || die "missing $f"; done
[ "${DB_ONLY}" = 1 ] || [ -f "${VOLUME}" ] || die "missing ${VOLUME}"

# A nightly backup (bin/backup.sh) carries facts.txt: sql/verify.sql's output at dump
# time. Then the expected shape comes from it, and every table's row count must match
# too. The Fly backup set has no facts file and uses the defaults above.
FACTS_FILE="${BACKUP_DIR}/facts.txt"
if [ -f "${FACTS_FILE}" ]; then
  from_facts() { awk -F'|' -v k="$1" '$1==k {print $2}' "${FACTS_FILE}"; }
  EXPECT_TABLES="$(from_facts tables)"
  EXPECT_FORCED_RLS="$(from_facts forced_rls_tables)"
  EXPECT_POLICIES="$(from_facts policies)"
  say "expected shape from facts.txt"
fi

# --- how SQL reaches the database ---------------------------------------------------
# On the box: the compose `postgres` service. In a rehearsal: a named container.
compose() { docker compose -f "${KIT_DIR}/docker-compose.yml" "$@"; }
pg_exec() {
  if [ -n "${CONTAINER}" ]; then
    docker exec -i "${CONTAINER}" "$@"
  else
    compose exec -T postgres "$@"
  fi
}
psql_q() { pg_exec psql -U persona -d persona -v ON_ERROR_STOP=1 -X -q -t -A "$@"; }

# --- 1. checksums ---------------------------------------------------------------------
if [ -f "${BACKUP_DIR}/SHA256SUMS" ]; then
  say "verifying checksums"
  (cd "${BACKUP_DIR}" && sha256sum --quiet -c SHA256SUMS) || die "checksum mismatch"
elif [ -f "${FACTS_FILE}" ]; then
  # A nightly backup always ends with SHA256SUMS; its absence means that night's backup
  # stopped part-way, and a half-written set must not be restored as if it were whole.
  die "facts.txt without SHA256SUMS: this nightly backup is incomplete"
else
  say "WARNING: no SHA256SUMS next to the backups; transfer integrity is unverified"
fi

# --- the persona_app password ---------------------------------------------------------
if [ -n "${APP_PASSWORD_FILE}" ]; then
  APP_PW="$(tr -d '\r\n' < "${APP_PASSWORD_FILE}")"
else
  [ -f "${OP_ENV_DIR}/postgres.env" ] || die "missing ${OP_ENV_DIR}/postgres.env"
  # render_env.py writes values single-quoted for Compose; strip the quotes.
  APP_PW="$(sed -n "s/^PERSONA_APP_DB_PASSWORD='\{0,1\}\([^']*\)'\{0,1\}\$/\1/p" "${OP_ENV_DIR}/postgres.env" | tr -d '\r\n')"
fi
# Generated passwords are hex (bin/fill_generated.sh), so they need no SQL quoting. A
# value that is not plain hex is refused rather than escaped.
printf '%s' "${APP_PW}" | grep -Eq '^[0-9a-f]{32,}$' \
  || die "PERSONA_APP_DB_PASSWORD is missing or not a generated hex value (run bin/fill_generated.sh)"

# --- the database is up and empty -------------------------------------------------------
if [ -z "${CONTAINER}" ]; then
  say "starting postgres alone"
  compose up -d postgres
fi
for _ in $(seq 1 60); do
  if pg_exec pg_isready -h 127.0.0.1 -U persona -d persona >/dev/null 2>&1; then break; fi
  sleep 2
done
pg_exec pg_isready -h 127.0.0.1 -U persona -d persona >/dev/null 2>&1 || die "postgres did not become ready"

existing="$(psql_q -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")" \
  || die "could not query the database"
[ "${existing}" = "0" ] || die "database already has ${existing} tables; refusing to restore over data"

# --- 3. roles -------------------------------------------------------------------------
# globals.sql is CHECKED, not replayed: replaying it would apply the old password hashes
# and the old CREATE ROLE persona (which the image already created). What must match is
# the attribute set, because RLS depends on persona_app NOT bypassing it.
app_attrs="$(grep -E '^ALTER ROLE persona_app WITH' "${GLOBALS}" | sed -E 's/ PASSWORD .*//')"
for attr in NOSUPERUSER NOBYPASSRLS LOGIN NOCREATEROLE NOCREATEDB NOREPLICATION; do
  printf '%s' "${app_attrs}" | grep -qw "${attr}" \
    || die "globals.sql persona_app lacks ${attr}; this is not the expected backup set"
done
grep -Eq '^CREATE ROLE persona;' "${GLOBALS}" || die "globals.sql does not define role persona"

say "creating persona_app (attributes from globals.sql, new password)"
# The password goes in on stdin, never on a command line. Idempotent: a role left behind
# by a failed earlier run (pg_restore's rollback does not remove roles) is brought to the
# same attributes and password instead of failing the re-run.
pg_exec psql -U persona -d persona -v ON_ERROR_STOP=1 -X -q <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'persona_app') THEN
    CREATE ROLE persona_app;
  END IF;
END
\$\$;
ALTER ROLE persona_app WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION INHERIT PASSWORD '${APP_PW}';
SQL
unset APP_PW

# --- 4. pgvector before data ------------------------------------------------------------
say "installing pgvector"
psql_q -c "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public" >/dev/null

# --- 5. data --------------------------------------------------------------------------
say "pg_restore (single transaction, exit on first error)"
pg_exec pg_restore -U persona -d persona --single-transaction --exit-on-error < "${DUMP}" \
  || die "pg_restore failed; the transaction rolled back, so no table was left behind and a re-run is safe"

# --- 6. verification --------------------------------------------------------------------
say "verifying"
facts="$(pg_exec psql -U persona -d persona -v ON_ERROR_STOP=1 -X -q -t -A -F '|' < "${KIT_DIR}/sql/verify.sql")" \
  || die "sql/verify.sql failed"
printf '%s\n' "${facts}" | sed 's/^/restore:   /' >&2
fact() { printf '%s\n' "${facts}" | awk -F'|' -v k="$1" '$1==k {print $2}'; }
[ "$(fact tables)" = "${EXPECT_TABLES}" ] || die "expected ${EXPECT_TABLES} tables, found $(fact tables)"
[ "$(fact forced_rls_tables)" = "${EXPECT_FORCED_RLS}" ] || die "expected ${EXPECT_FORCED_RLS} forced-RLS tables"
[ "$(fact policies)" = "${EXPECT_POLICIES}" ] || die "expected ${EXPECT_POLICIES} policies"
[ "$(fact persona_app_bypassrls)" = "false" ] || die "persona_app bypasses RLS"
[ "$(fact persona_app_superuser)" = "false" ] || die "persona_app is a superuser"
[ "$(fact persona_app_tables_without_select)" = "0" ] || die "persona_app lacks SELECT on some tables"
[ "$(fact vector_extension)" != "" ] || die "vector extension missing"
[ -n "$(fact alembic_revision)" ] || die "alembic_version is empty"
if [ -f "${FACTS_FILE}" ]; then
  # facts.txt is taken moments before the dump while the box keeps writing, so small
  # drift is normal and only warned about; read the diff, an emptied table is not drift.
  if diff <(grep '^rows\.' "${FACTS_FILE}") <(printf '%s\n' "${facts}" | grep '^rows\.') >&2; then
    say "row counts match facts.txt for every table"
  else
    say "WARNING: row counts differ from facts.txt (diff above); check no table came back empty"
  fi
fi

# RLS fails CLOSED for persona_app with no user set: every RLS table reads as empty.
rls_leak="$(pg_exec psql -U persona -d persona -v ON_ERROR_STOP=1 -X -q -t -A <<'SQL'
SET ROLE persona_app;
SELECT count(*) FROM personas;
SQL
)" || die "the RLS probe as persona_app failed (a policy that errors with no user set?)"
[ "${rls_leak}" = "0" ] || die "persona_app saw ${rls_leak} personas with no user set; RLS is not enforcing"
say "RLS check passed: persona_app with no user set sees 0 personas"

# --- 7. the api volume ----------------------------------------------------------------
if [ "${DB_ONLY}" = 1 ]; then
  say "database restored and verified (--db-only: volume skipped)"
  exit 0
fi
target="${OP_DATA_DIR}/persona"
sudo mkdir -p "${target}"
if [ -n "$(sudo ls -A "${target}")" ]; then
  die "${target} is not empty; refusing to unpack over it"
fi
say "unpacking the api volume into ${target}"
# --numeric-owner keeps uid 1000, the image's `persona` user. Modes are kept as archived.
sudo tar -xzf "${VOLUME}" --numeric-owner --same-permissions -C "${target}"
sudo chown -R 1000:1000 "${target}"
for entry in audit workspace tools mcp-mirror; do
  sudo test -e "${target}/${entry}" || die "volume entry ${entry} missing after unpack"
done
say "volume entries: $(sudo find "${target}" | wc -l) (the backup listed 214)"
say "done. Next: bin/deploy.sh (it runs the migrations to head)."
