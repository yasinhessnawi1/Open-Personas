#!/usr/bin/env bash
# Capture every Fly secret VALUE while the machines are still running.
#
# Fly secrets are write-only: `flyctl secrets list` shows names and digests, never
# values. The only way to read one back is to ask a RUNNING machine to print its
# own environment. Once a machine is destroyed, anything not captured here and not
# present in a provider dashboard is GONE.
#
# `APP_DATABASE_URL` is the sharpest case: it carries the persona_app password
# rotated 2026-08-09 and exists nowhere else. Local .env still points at the
# decommissioned Neon instance (R9-110), so .env is NOT a backup of it.
#
# Usage:  ./capture-fly-secrets.sh /secure/path/outside/the/repo
#
# The output is 165 live credentials in one file. Treat it as a vault export:
# write it to an encrypted volume, never into the repo, and delete it once the
# new host is provisioned and verified.
set -euo pipefail

OUT="${1:?usage: capture-fly-secrets.sh <output-dir-OUTSIDE-the-repo>}"
case "$(cd "$OUT" 2>/dev/null && pwd || echo "")" in
  "$(git rev-parse --show-toplevel 2>/dev/null || echo __none__)"*)
    echo "refusing: $OUT is inside the git repo. Pick a path outside it." >&2
    exit 1 ;;
esac
mkdir -p "$OUT"
chmod 700 "$OUT"

APPS=(open-persona-api open-persona-connectors open-persona-voice open-persona-livekit)

for app in "${APPS[@]}"; do
  echo "==> $app"
  names="$(flyctl secrets list -a "$app" 2>/dev/null | tail -n +2 | awk '{print $1}' | grep -v '^$' || true)"
  if [ -z "$names" ]; then
    echo "    no secrets listed (app unreachable?) — SKIPPING, do not assume empty" >&2
    continue
  fi

  dest="$OUT/$app.env"
  : > "$dest"
  chmod 600 "$dest"

  # One ssh session for all names: printenv prints "NAME=value" per line with -0
  # avoided for readability. Values containing newlines (PEM keys!) are handled by
  # asking for them one at a time below.
  missed=""
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    val="$(flyctl ssh console -a "$app" -C "printenv $name" 2>/dev/null \
            | grep -v '^Connecting' | sed '/^$/d' || true)"
    if [ -z "$val" ]; then
      missed="$missed $name"
      continue
    fi
    # Multi-line values (JWT PEM keys) are written as-is inside single quotes.
    if [ "$(printf '%s' "$val" | wc -l)" -gt 0 ]; then
      printf "%s='%s'\n" "$name" "$val" >> "$dest"
    else
      printf "%s=%s\n" "$name" "$val" >> "$dest"
    fi
  done <<< "$names"

  count="$(grep -c '=' "$dest" || true)"
  total="$(printf '%s\n' "$names" | grep -c . || true)"
  echo "    captured $count of $total → $dest"
  [ -n "$missed" ] && echo "    NOT CAPTURED (check manually):$missed" >&2
done

echo
echo "Verify before destroying anything:"
echo "  grep -c = $OUT/*.env"
echo "  grep APP_DATABASE_URL $OUT/open-persona-api.env   # must be present + non-empty"
