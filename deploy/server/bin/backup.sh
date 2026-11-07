#!/usr/bin/env bash
# Open Persona: nightly backup on the core host (systemd timer open-persona-backup).
#
# Writes, into ${OP_DATA_DIR}/backups/nightly/<UTC date>/:
#   persona.dump         pg_dump -Fc of the `persona` database (the same shape as the Fly
#                        backup, so bin/restore.sh restores it unchanged)
#   globals.sql          roles (pg_dumpall --globals-only)
#   persona_volume.tgz   the api data directory minus the HF model cache
#   facts.txt            schema shape and row counts (bin/restore.sh checks against it)
#   SHA256SUMS
# and keeps the newest ${KEEP_DAYS} days. A backup on the same disk is not a backup of the
# disk: each night's directory must also leave the host. The target is configuration, not
# code (ruling D4: Azure Blob with an immutability policy, a write-only SAS and age
# encryption, provisioned by the Terraform session): OFFSITE_CMD, set in
# /opt/open-persona/env/offsite.env, is a command that receives the directory path as its
# argument. Until it is set the backup runs locally and says the off-site copy is PENDING.

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_DIR="$(dirname "${SCRIPT_DIR}")"
export OP_ENV_DIR="${OP_ENV_DIR:-/opt/open-persona/env}"
OP_DATA_DIR="${OP_DATA_DIR:-/srv/open-persona}"
KEEP_DAYS="${KEEP_DAYS:-14}"
OFFSITE_CMD="${OFFSITE_CMD:-}"

# shellcheck source=lib/compose.sh
. "${SCRIPT_DIR}/lib/compose.sh"
require_data_mount

# Below this much free space the backup refuses to start rather than fill the disk the
# database lives on (the host profile sets it). Any failure exits non-zero, and the unit's
# OnFailure= raises an alert.
MIN_FREE_GB="${OP_BACKUP_MIN_FREE_GB:-20}"

mkdir -p "${OP_DATA_DIR}/backups/nightly"
free_gb="$(df --output=avail -BG "${OP_DATA_DIR}/backups" | tail -n 1 | tr -dc '0-9')"
if [ "${free_gb}" -lt "${MIN_FREE_GB}" ]; then
  echo "backup: REFUSED: ${free_gb} GB free under ${OP_DATA_DIR}/backups, need ${MIN_FREE_GB} GB" >&2
  exit 1
fi

dest="${OP_DATA_DIR}/backups/nightly/$(date -u +%Y-%m-%d)"
mkdir -p "${dest}"

# facts.txt: the schema shape and per-table row counts, taken just before the dump.
# restore.sh checks the shape strictly. Row counts are compared as a WARNING only: the
# scheduler and the job worker keep writing during the backup, so a few rows of drift
# between the two moments is normal; a table that comes back empty is not.
compose exec -T postgres psql -U persona -d persona -X -q -t -A -F '|' \
  < "${KIT_DIR}/sql/verify.sql" > "${dest}/facts.txt"
compose exec -T postgres pg_dump -U persona -Fc persona > "${dest}/persona.dump"
compose exec -T postgres pg_dumpall -U persona --globals-only > "${dest}/globals.sql"
# GNU tar exits 1 for "file changed as we read it", which is normal on a live directory
# (audit and turn logs are appended continuously). Exit 2 and above are real failures.
tar -czf "${dest}/persona_volume.tgz" --numeric-owner --exclude=./hf \
  -C "${OP_DATA_DIR}/persona" . || [ $? -eq 1 ]
(cd "${dest}" && sha256sum facts.txt persona.dump globals.sql persona_volume.tgz > SHA256SUMS)

# A dump that pg_restore cannot list is not a backup.
compose exec -T postgres pg_restore -l < "${dest}/persona.dump" > /dev/null

find "${OP_DATA_DIR}/backups/nightly" -mindepth 1 -maxdepth 1 -type d -mtime "+${KEEP_DAYS}" \
  -exec rm -rf {} +
find "${OP_DATA_DIR}/backups" -maxdepth 1 -name 'pre-deploy-*.dump' -mtime "+${KEEP_DAYS}" -delete

# The off-site target is configuration (OFFSITE_CMD from /opt/open-persona/env/offsite.env,
# read by the systemd unit). Until it exists the backup still runs, locally, and says so on
# every run; RUNBOOK.md, "Backups off the host", tracks it.
if [ -n "${OFFSITE_CMD}" ]; then
  ${OFFSITE_CMD} "${dest}"
else
  echo "backup: off-site copy PENDING: no OFFSITE_CMD is configured; this set exists only on this host" >&2
fi
echo "backup: wrote ${dest} ($(du -sh "${dest}" | cut -f1))"
