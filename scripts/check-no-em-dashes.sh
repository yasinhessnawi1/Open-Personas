#!/usr/bin/env bash
# scripts/check-no-em-dashes.sh
#
# Owner rule (2026): em dashes and en dashes are forbidden in user-facing text.
# They read as machine-written and cost us trust with the people we are trying
# to sign up as users and testers.
#
# Scope is deliberately the surfaces a real person reads:
#   1. Public markdown: root README + CHANGELOG, every package README/CHANGELOG,
#      and the builtin skill prompts.
#   2. packages/web/src/i18n/messages/en.json, the single home of every
#      user-visible string in the web app once it flows through next-intl.
#      Gating the message catalogue (not the .tsx files) keeps the check exact:
#      code comments are free to say whatever they like.
#
# docs/ is intentionally excluded: gitignored, private, same tier as .env.
#
# Usage: bash scripts/check-no-em-dashes.sh

set -euo pipefail
cd "$(dirname "$0")/.."

# An ALTERNATION of the two literal characters, never a bracket expression.
# `[—–]` is a set of BYTES wherever grep is not running under a UTF-8 locale, so it
# matched the shared lead bytes of every other U+20xx character (curly quotes,
# ellipses) and counted each real dash three times, once per byte. On a shell with
# no LANG/LC_ALL set that reported ~3500 violations against a repo with none, and
# under UTF-8 the same script reported clean: a copy gate whose verdict depended on
# the ambient locale. Alternation matches each whole multi-byte character in either
# locale, so the count is the true one.
DASH='—|–'
violations=0
report() {
  local file="$1" count="$2"
  printf '   %4d  %s\n' "$count" "$file"
}

echo "Checking user-facing text for em/en dashes..."
echo

offenders=()
while IFS= read -r f; do
  [[ -f "$f" ]] || continue
  # `|| true`: grep exits 1 on no-match, which pipefail would turn into a
  # script abort. A clean file is the expected case, not an error.
  #
  # packages/web/COPY.md is the rule itself, so its counter-example lines have
  # to contain the very thing they warn against. Those lines are marked with a
  # leading "no" column; everything else in the file is still gated, so the doc
  # cannot quietly become a place where bad copy hides.
  if [[ "$f" == "packages/web/COPY.md" ]]; then
    body=$(grep -vE '^no[[:space:]]{2,}' "$f" || true)
  else
    body=$(cat "$f")
  fi
  count=$( { printf '%s' "$body" | grep -oE "$DASH" || true; } | wc -l | tr -d ' ')
  if [[ "$count" != "0" ]]; then
    offenders+=("$f:$count")
    violations=$((violations + count))
  fi
done < <(
  git ls-files '*.md' ':!:docs/*'
  git ls-files 'packages/web/src/i18n/messages/en.json'
)

if [[ ${#offenders[@]} -gt 0 ]]; then
  echo "Found $violations em/en dash(es) in user-facing text:"
  echo
  for entry in "${offenders[@]}"; do
    report "${entry%:*}" "${entry##*:}"
  done
  echo
  echo "Rewrite them. A period, comma, colon, or parentheses almost always reads better."
  echo "See the project rule: no em dashes in anything a user can see."
  exit 1
fi

echo "Clean. No em or en dashes in user-facing text."
