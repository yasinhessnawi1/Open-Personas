#!/usr/bin/env bash
# Open Persona: daily disk check (open-persona-diskcheck.timer). Exits 1 when any watched
# filesystem is fuller than OP_DISK_ALERT_PCT (default 85), which raises an alert through
# the unit's OnFailure=. Watched: /, Docker's data root, and the data and backup trees.

set -euo pipefail

OP_DATA_DIR="${OP_DATA_DIR:-/srv/open-persona}"
LIMIT="${OP_DISK_ALERT_PCT:-85}"
docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"

over=0
for path in / "${docker_root}" "${OP_DATA_DIR}" "${OP_DATA_DIR}/backups"; do
  [ -e "${path}" ] || continue
  used="$(df --output=pcent "${path}" | tail -n 1 | tr -dc '0-9')"
  if [ "${used}" -ge "${LIMIT}" ]; then
    echo "diskcheck: ${path} is ${used}% full (limit ${LIMIT}%)" >&2
    over=1
  else
    echo "diskcheck: ${path} ${used}%"
  fi
done
exit "${over}"
