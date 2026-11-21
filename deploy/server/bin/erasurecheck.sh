#!/usr/bin/env bash
# Open Persona: erasure check (open-persona-erasurecheck.timer, hourly). Exits 1 when an
# erasure has failed or has been unfinished for 15 minutes or more, which raises an alert
# through the unit's OnFailure= (R9-284). The api resumes such erasures on its own; this is
# for the operator to look when that is not enough. Ids and counts only, never content.
# It also counts persona creates left unfinished for 30 minutes or more (R9-293): the api's
# sweep claims and erases one after 15, so one still there means that sweep is not running.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STALL_MINUTES="${OP_ERASURE_STALL_MINUTES:-15}"
CREATE_MINUTES="${OP_ABANDONED_CREATE_MINUTES:-30}"
case "${STALL_MINUTES}" in
  ''|*[!0-9]*) echo "erasurecheck: OP_ERASURE_STALL_MINUTES must be a whole number" >&2; exit 2 ;;
esac
case "${CREATE_MINUTES}" in
  ''|*[!0-9]*) echo "erasurecheck: OP_ABANDONED_CREATE_MINUTES must be a whole number" >&2; exit 2 ;;
esac

STALLED_SQL="SELECT count(*) FROM erasures WHERE state = 'failed' OR (state <> 'complete' AND requested_at < now() - interval '${STALL_MINUTES} minutes')"
ABANDONED_SQL="SELECT count(*) FROM personas WHERE NOT create_complete AND created_at < now() - interval '${CREATE_MINUTES} minutes'"

counts="$("${SCRIPT_DIR}/compose.sh" exec -T postgres psql -U persona -d persona -X -q -t -A \
  -c "SELECT (${STALLED_SQL}) || ' ' || (${ABANDONED_SQL})")" \
  || { echo "erasurecheck: could not query the database" >&2; exit 1; }
read -r stalled abandoned <<<"${counts}" || true
case "${stalled:-x}/${abandoned:-x}" in
  *[!0-9/]*) echo "erasurecheck: unexpected answer from the database" >&2; exit 1 ;;
esac

status=0
if [ "${stalled}" != "0" ]; then
  echo "erasurecheck: ${stalled} erasure(s) failed or unfinished for ${STALL_MINUTES}+ minutes; see RUNBOOK.md, Alerts" >&2
  status=1
fi
if [ "${abandoned}" != "0" ]; then
  echo "erasurecheck: ${abandoned} persona create(s) unfinished for ${CREATE_MINUTES}+ minutes; see RUNBOOK.md, Alerts" >&2
  status=1
fi
if [ "${status}" = 0 ]; then
  echo "erasurecheck: no stalled erasures and no abandoned persona creates"
fi
exit "${status}"
