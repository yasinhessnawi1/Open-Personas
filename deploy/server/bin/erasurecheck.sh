#!/usr/bin/env bash
# Open Persona: erasure check (open-persona-erasurecheck.timer, hourly). Exits 1 when an
# erasure has failed or has been unfinished for 15 minutes or more, which raises an alert
# through the unit's OnFailure= (R9-284). The api resumes such erasures on its own; this is
# for the operator to look when that is not enough. Ids and counts only, never content.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STALL_MINUTES="${OP_ERASURE_STALL_MINUTES:-15}"
case "${STALL_MINUTES}" in
  ''|*[!0-9]*) echo "erasurecheck: OP_ERASURE_STALL_MINUTES must be a whole number" >&2; exit 2 ;;
esac

stalled="$("${SCRIPT_DIR}/compose.sh" exec -T postgres psql -U persona -d persona -X -q -t -A \
  -c "SELECT count(*) FROM erasures WHERE state = 'failed' OR (state <> 'complete' AND requested_at < now() - interval '${STALL_MINUTES} minutes')")" \
  || { echo "erasurecheck: could not query the database" >&2; exit 1; }

if [ "${stalled}" != "0" ]; then
  echo "erasurecheck: ${stalled} erasure(s) failed or unfinished for ${STALL_MINUTES}+ minutes; see RUNBOOK.md, Erasures" >&2
  exit 1
fi
echo "erasurecheck: no stalled erasures"
