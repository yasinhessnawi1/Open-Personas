#!/usr/bin/env bash
# Open Persona: raise an alert. Called by systemd (open-persona-alert@.service, the
# OnFailure= of the backup and disk-check units) and usable by hand.
#
#   bin/alert.sh <source> <message>   record and notify
#   bin/alert.sh --ack                acknowledge: move the marker aside once handled
#
# Every alert is appended to the marker file ${OP_ALERT_DIR}/ALERTS, which the login
# banner (/etc/update-motd.d/99-open-persona-alerts) and bin/preflight.sh report until it
# is acknowledged. That marker is the default and always happens. If alert.env sets
# OP_ALERT_CMD, it also runs, as `sh -c "$OP_ALERT_CMD" alert "<the alert line>"`, so the
# line is "$1" inside the command: a webhook (curl to a chat or uptime service) or a mail
# command, the owner's choice (RUNBOOK.md, "Alerts"). A failing notifier is itself
# recorded in the marker; the marker never depends on the network.

set -uo pipefail
umask 007

OP_ENV_DIR="${OP_ENV_DIR:-/opt/open-persona/env}"
OP_ALERT_DIR="${OP_ALERT_DIR:-/var/lib/open-persona/alerts}"
MARKER="${OP_ALERT_DIR}/ALERTS"

if [ "${1:-}" = "--ack" ]; then
  if [ -s "${MARKER}" ]; then
    mv "${MARKER}" "${OP_ALERT_DIR}/ACKED-$(date -u +%Y%m%dT%H%M%SZ)"
    echo "alert: acknowledged"
  else
    echo "alert: nothing to acknowledge"
  fi
  exit 0
fi

source_name="${1:-manual}"
message="${2:-no message}"
line="$(date -u +%Y-%m-%dT%H:%M:%SZ) $(hostname) ${source_name}: ${message}"
mkdir -p "${OP_ALERT_DIR}"
printf '%s\n' "${line}" >> "${MARKER}"
echo "alert: recorded: ${line}" >&2

cmd=""
if [ -r "${OP_ENV_DIR}/alert.env" ]; then
  cmd="$(sed -n "s/^OP_ALERT_CMD='\{0,1\}\(.*\)'\{0,1\}\$/\1/p" "${OP_ENV_DIR}/alert.env" | tail -n 1)"
  cmd="${cmd%\'}"
fi
if [ -n "${cmd}" ]; then
  if ! timeout 30 sh -c "${cmd}" alert "${line}"; then
    printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(hostname) alert.sh: the notifier command failed" >> "${MARKER}"
  fi
fi
exit 0
