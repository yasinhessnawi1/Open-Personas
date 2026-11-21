#!/usr/bin/env bash
# Open Persona: erasure check (open-persona-erasurecheck.timer, hourly). Exits 1 when an
# erasure has failed or has been unfinished for 15 minutes or more, which raises an alert
# through the unit's OnFailure= (R9-284). The api resumes such erasures on its own; this is
# for the operator to look when that is not enough. Ids and counts only, never content.
# It also counts persona creates left unfinished for 30 minutes or more (R9-293): the api's
# sweep claims and erases one after 15, so one still there means that sweep is not running.
#
# Each count is its own psql call, so one that fails never hides the other (R9-293 round 3).
# Before migration 069 has added personas.create_complete (a deploy between its build and its
# migrate), the abandoned-create count is skipped with a notice, not failed.

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
COLUMN_SQL="SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'personas' AND column_name = 'create_complete'"
ABANDONED_SQL="SELECT count(*) FROM personas WHERE NOT create_complete AND created_at < now() - interval '${CREATE_MINUTES} minutes'"

# One count, as a whole number on stdout; any failure or other answer returns non-zero.
count() {
  local answer
  answer="$("${SCRIPT_DIR}/compose.sh" exec -T postgres psql -U persona -d persona -X -q -t -A -c "$1")" \
    || return 1
  case "${answer}" in
    ''|*[!0-9]*) return 1 ;;
  esac
  echo "${answer}"
}

status=0
quiet=1

if stalled="$(count "${STALLED_SQL}")"; then
  if [ "${stalled}" != "0" ]; then
    echo "erasurecheck: ${stalled} erasure(s) failed or unfinished for ${STALL_MINUTES}+ minutes; see RUNBOOK.md, Alerts" >&2
    status=1
  fi
else
  echo "erasurecheck: could not count stalled erasures" >&2
  status=1
fi

if column="$(count "${COLUMN_SQL}")"; then
  if [ "${column}" = "0" ]; then
    echo "erasurecheck: personas.create_complete is not migrated yet (069); abandoned persona creates not counted"
    quiet=0
  elif abandoned="$(count "${ABANDONED_SQL}")"; then
    if [ "${abandoned}" != "0" ]; then
      echo "erasurecheck: ${abandoned} persona create(s) unfinished for ${CREATE_MINUTES}+ minutes; see RUNBOOK.md, Alerts" >&2
      status=1
    fi
  else
    echo "erasurecheck: could not count abandoned persona creates" >&2
    status=1
  fi
else
  echo "erasurecheck: could not count abandoned persona creates" >&2
  status=1
fi

if [ "${status}" = 0 ] && [ "${quiet}" = 1 ]; then
  echo "erasurecheck: no stalled erasures and no abandoned persona creates"
fi
exit "${status}"
