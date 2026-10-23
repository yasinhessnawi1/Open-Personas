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
#   3. Roles, all with NEW passwords (the Fly-era hashes in globals.sql are never applied):
#      * persona: the superuser, created by the postgres image (POSTGRES_PASSWORD). Only
#        the migrate service and these scripts use it.
#      * persona_app: the RLS-enforced request-path role, with the attributes globals.sql
#        records (checked, not replayed).
#      * persona_platform: the api's long-running cross-tenant role (admin, dispatch and
#        JIT-provisioning engines). BYPASSRLS but NOT a superuser, DML only, no DDL, so
#        a compromised api cannot COPY TO PROGRAM, alter roles or read server files.
#   4. Installs pgvector BEFORE the data (the dump's CREATE EXTENSION needs it).
#   5. pg_restore in one transaction, stopping at the first error.
#   6. persona_platform's DML grants and default privileges (packages/api/release.py
#      re-applies them on every deploy).
#   7. Runs sql/verify.sql and compares the facts with this backup set's known shape.
#   8. Unpacks the api volume tarball into the api's data directory (skipped with
#      --db-only), after scanning it: regular files and directories only, no absolute or
#      `..` paths. It extracts as uid 1000, never as root, and takes neither owners nor
#      modes from the archive.
# Migrations to the current head are NOT run here. bin/deploy.sh runs them through the
# `migrate` service, so the restored data meets the code exactly the way every deploy
# does.
#
# Usage on the box (from the checkout's deploy/hetzner directory):
#   bin/restore.sh --backup-dir /srv/open-persona/restore
#   bin/restore.sh --roles-only     (re)apply step 3 and 6 to an existing database, e.g.
#                                   after a password rotation; touches no data
#   bin/restore.sh --backup-dir DIR --volume-only
#                                   steps 1 and 8 alone: the database is not touched
# Rehearsal against a throwaway container (no compose, no volume):
#   bin/restore.sh --backup-dir DIR --container NAME --password-dir DIR --db-only
#   (--password-dir holds files app_pw and platform_pw)
#
# Never paste pg_restore's error output anywhere public: a failed COPY can echo row data.
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
DATA_UID=1000   # the images' `persona` user

BACKUP_DIR=""
CONTAINER=""
PASSWORD_DIR=""
DB_ONLY=0
ROLES_ONLY=0
VOLUME_ONLY=0
OP_ENV_DIR="${OP_ENV_DIR:-/opt/open-persona/env}"
OP_DATA_DIR="${OP_DATA_DIR:-/srv/open-persona}"

die() { echo "restore: FAILED: $*" >&2; exit 1; }
say() { echo "restore: $*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
    --container) CONTAINER="$2"; shift 2 ;;
    --password-dir) PASSWORD_DIR="$2"; shift 2 ;;
    --db-only) DB_ONLY=1; shift ;;
    --roles-only) ROLES_ONLY=1; shift ;;
    --volume-only) VOLUME_ONLY=1; shift ;;
    -h|--help) sed -n '2,46p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

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

# --- passwords --------------------------------------------------------------------------
# From postgres.env on the box (render_env.py writes values single-quoted for Compose;
# the quotes are stripped), or from --password-dir in a rehearsal.
read_password() {  # read_password <env name> <rehearsal file>
  local value
  if [ -n "${PASSWORD_DIR}" ]; then
    value="$(tr -d '\r\n' < "${PASSWORD_DIR}/$2")"
  else
    [ -f "${OP_ENV_DIR}/postgres.env" ] || die "missing ${OP_ENV_DIR}/postgres.env"
    value="$(sed -n "s/^$1='\{0,1\}\([^']*\)'\{0,1\}\$/\1/p" "${OP_ENV_DIR}/postgres.env" | tr -d '\r\n')"
  fi
  # Generated passwords are hex (bin/fill_generated.sh), so they need no SQL quoting. A
  # value that is not plain hex is refused rather than escaped.
  printf '%s' "${value}" | grep -Eq '^[0-9a-f]{32,}$' \
    || die "$1 is missing or not a generated hex value (run bin/fill_generated.sh)"
  printf '%s' "${value}"
}

wait_for_postgres() {
  if [ -z "${CONTAINER}" ]; then
    say "starting postgres"
    compose up -d postgres
  fi
  for _ in $(seq 1 60); do
    if pg_exec pg_isready -h 127.0.0.1 -U persona -d persona >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  die "postgres did not become ready"
}

# Step 3. Idempotent: a role left behind by a failed earlier run (pg_restore's rollback
# does not remove roles) is brought to the required attributes and password. Passwords go
# in on stdin, never on a command line, and the session stops Postgres from logging a
# failing statement (which would carry the password) before the ALTERs.
apply_roles() {
  local app_pw platform_pw
  app_pw="$(read_password PERSONA_APP_DB_PASSWORD app_pw)"
  platform_pw="$(read_password PERSONA_PLATFORM_DB_PASSWORD platform_pw)"
  say "roles: persona_app (RLS-enforced), persona_platform (BYPASSRLS, not superuser)"
  pg_exec psql -U persona -d persona -v ON_ERROR_STOP=1 -X -q <<SQL
SET log_min_error_statement = panic;
SET log_statement = 'none';
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'persona_app') THEN
    CREATE ROLE persona_app;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'persona_platform') THEN
    CREATE ROLE persona_platform;
  END IF;
END
\$\$;
ALTER ROLE persona_app WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION INHERIT PASSWORD '${app_pw}';
ALTER ROLE persona_platform WITH LOGIN NOSUPERUSER BYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION INHERIT PASSWORD '${platform_pw}';
SQL
}

# Step 6. The same statements packages/api/release.py runs on every deploy.
apply_platform_grants() {
  say "persona_platform: DML on every table and sequence, and the default privileges"
  psql_q <<'SQL' >/dev/null
GRANT CONNECT ON DATABASE persona TO persona_platform;
GRANT USAGE ON SCHEMA public TO persona_platform;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO persona_platform;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO persona_platform;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO persona_platform;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO persona_platform;
SQL
}

if [ "${ROLES_ONLY}" = 1 ]; then
  wait_for_postgres
  apply_roles
  apply_platform_grants
  say "roles and grants applied; no data touched"
  exit 0
fi

[ -n "${BACKUP_DIR}" ] || die "--backup-dir is required"
DUMP="${BACKUP_DIR}/persona.dump"
GLOBALS="${BACKUP_DIR}/globals.sql"
VOLUME="${BACKUP_DIR}/persona_volume.tgz"
if [ "${VOLUME_ONLY}" = 0 ]; then
  for f in "${DUMP}" "${GLOBALS}"; do [ -f "$f" ] || die "missing $f"; done
fi
[ "${DB_ONLY}" = 1 ] || [ -f "${VOLUME}" ] || die "missing ${VOLUME}"

# A nightly backup (bin/backup.sh) carries facts.txt: sql/verify.sql's output at dump
# time. Then the expected shape comes from it. The Fly backup set has no facts file and
# uses the defaults above.
FACTS_FILE="${BACKUP_DIR}/facts.txt"
if [ -f "${FACTS_FILE}" ]; then
  from_facts() { awk -F'|' -v k="$1" '$1==k {print $2}' "${FACTS_FILE}"; }
  EXPECT_TABLES="$(from_facts tables)"
  EXPECT_FORCED_RLS="$(from_facts forced_rls_tables)"
  EXPECT_POLICIES="$(from_facts policies)"
  say "expected shape from facts.txt"
fi

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

# --- the volume is safe to unpack (checked BEFORE touching the database) ----------------
scan_volume() {
  # Every entry must be a regular file or a directory (no symlinks, hardlinks or device
  # nodes), and every name relative and free of `..`.
  local bad_types bad_names
  bad_types="$(tar -tvzf "${VOLUME}" | awk '{t=substr($1,1,1); if (t != "-" && t != "d") print}' || true)"
  [ -z "${bad_types}" ] || die "the volume archive holds an entry that is not a file or directory"
  bad_names="$(tar -tzf "${VOLUME}" | grep -E '^/|(^|/)\.\.(/|$)' || true)"
  [ -z "${bad_names}" ] || die "the volume archive holds an absolute or .. path"
  say "volume archive scan passed: regular files and directories only"
}

# Step 8. As uid 1000, never root: the archive cannot create anything uid 1000 could not.
# No owners and no modes come from the archive; the umask decides (owner rw, group r).
unpack_volume() {
  local target="${OP_DATA_DIR}/persona"
  sudo install -d -o "${DATA_UID}" -g "${DATA_UID}" -m 0750 "${target}"
  if [ -n "$(sudo ls -A "${target}")" ]; then
    die "${target} is not empty; refusing to unpack over it"
  fi
  say "unpacking the api volume into ${target} as uid ${DATA_UID}"
  sudo setpriv --reuid="${DATA_UID}" --regid="${DATA_UID}" --clear-groups \
    sh -c 'umask 027 && exec tar -xzf - --no-same-owner --no-same-permissions -C "$1"' sh "${target}" \
    < "${VOLUME}" || die "unpacking the volume failed"
  for entry in audit workspace tools mcp-mirror; do
    sudo test -e "${target}/${entry}" || die "volume entry ${entry} missing after unpack"
  done
  say "volume entries: $(sudo find "${target}" | wc -l) (the backup listed 214)"
}

if [ "${DB_ONLY}" = 0 ]; then
  scan_volume
fi
if [ "${VOLUME_ONLY}" = 1 ]; then
  unpack_volume
  say "volume restored (--volume-only: the database was not touched)"
  exit 0
fi

# --- 2. the database is up and empty ----------------------------------------------------
wait_for_postgres
existing="$(psql_q -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")" \
  || die "could not query the database"
[ "${existing}" = "0" ] || die "database already has ${existing} tables; refusing to restore over data"

# --- 3. roles -------------------------------------------------------------------------
# globals.sql is CHECKED, not replayed. What must match is the attribute set, because
# RLS depends on persona_app NOT bypassing it.
app_attrs="$(grep -E '^ALTER ROLE persona_app WITH' "${GLOBALS}" | sed -E 's/ PASSWORD .*//')"
for attr in NOSUPERUSER NOBYPASSRLS LOGIN NOCREATEROLE NOCREATEDB NOREPLICATION; do
  printf '%s' "${app_attrs}" | grep -qw "${attr}" \
    || die "globals.sql persona_app lacks ${attr}; this is not the expected backup set"
done
grep -Eq '^CREATE ROLE persona;' "${GLOBALS}" || die "globals.sql does not define role persona"
apply_roles

# --- 4. pgvector before data ------------------------------------------------------------
say "installing pgvector"
psql_q -c "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public" >/dev/null

# --- 5. data --------------------------------------------------------------------------
say "pg_restore (single transaction, exit on first error)"
pg_exec pg_restore -U persona -d persona --single-transaction --exit-on-error < "${DUMP}" \
  || die "pg_restore failed; the transaction rolled back, so no table was left behind and a re-run is safe"

# --- 6. the platform role's grants ------------------------------------------------------
apply_platform_grants

# --- 7. verification --------------------------------------------------------------------
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
[ "$(fact persona_platform_superuser)" = "false" ] || die "persona_platform is a superuser"
[ "$(fact persona_platform_tables_without_dml)" = "0" ] || die "persona_platform lacks DML on some tables"
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

# persona_platform can read across tenants but cannot do what a superuser can.
platform_ddl="$(pg_exec psql -U persona -d persona -X -q -t -A <<'SQL' 2>&1 || true
SET ROLE persona_platform;
CREATE TABLE restore_probe_must_fail (x int);
SQL
)"
printf '%s' "${platform_ddl}" | grep -q 'permission denied' \
  || die "persona_platform could create a table; it must have DML only"
say "platform check passed: persona_platform has no DDL"

# --- 8. the api volume ----------------------------------------------------------------
if [ "${DB_ONLY}" = 1 ]; then
  say "database restored and verified (--db-only: volume skipped)"
  exit 0
fi
unpack_volume
say "done. Next: bin/deploy.sh (it runs the migrations to head)."
