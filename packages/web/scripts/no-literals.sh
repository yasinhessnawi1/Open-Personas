#!/usr/bin/env bash
# packages/web/scripts/no-literals.sh
#
# Spec F2 T02 — CI no-literals gate (D-F2-6).
#
# Enforces criterion #2: no component hard-codes a design value. Catches:
#   1. Inline colour literals in Tailwind arbitrary-value utilities
#      (text-[#hex] / bg-[oklch(...)] / border-[rgb(...)] etc.)
#   2. Typography-sizing literals (text-[Nrem|em|px]) — except allowlist
#
# Allowlist: three F1+audit-documented exceptions. Legacy entries (the 5x
# text-[0.65rem] not yet closed by T16/T28/T30) are tracked here too; the
# legacy list shrinks as those tasks land.
#
# Scope (per audit.md §grep-gate-seed): src/components/**, src/app/(app)/**,
# src/app/reference/**, src/app/page.tsx. Excludes src/app/scratch/** (dev-only,
# NODE_ENV-guarded harness) and src/lib/ (token-definition source of truth +
# contrast test fixtures).
#
# Decision lineage: D-F2-6 — Biome noRestrictedSyntax fallback because the gate
# needs file:line-anchored allowlists that Biome's selector-based rule cannot
# express cleanly. Per Phase 5 T01 audit findings.

set -euo pipefail

cd "$(dirname "$0")/.."  # packages/web

# Forbidden patterns (regex, extended).
#   - Colour literals: (text|bg|border|ring|fill|stroke|outline|decoration)-[(#|oklch(|rgb(|hsl()
#   - Typography sizing: text-[\d+(.\d+)?(rem|em|px)]
FORBIDDEN_COLOUR='\b(text|bg|border|ring|fill|stroke|outline|decoration)-\[(#|oklch\(|rgb\(|hsl\()'
FORBIDDEN_TEXT='\btext-\[[0-9]+(\.[0-9]+)?(rem|em|px)\]'

# Documented allowlist (file:line — exact match required).
# These are deliberate F1 + audit-surfaced exceptions; DESIGN.md tracks rationale.
# NB: line numbers shift when files are edited (e.g., adding comments above
# the consuming line). Update here whenever the allowlisted line moves; the
# F2 T03/T11 retokenise edits shifted markdown.tsx :71→:79 and button.tsx :26→:31.
ALLOWLIST=(
  'src/components/persona/persona-avatar.tsx:67'    # text-[0.6rem] — avatar sm size (F1 closeout #12); :45→:67 via the avatar-render fix (internalWorkspacePath helper + AuthedAvatarImage import above SIZE_CLASSES)
  'src/components/ui/markdown.tsx:124'              # text-[0.8em] — inline-code relative (F1 closeout #12); :79→:81→:102→:124 via Spec 28 sanitize imports + img-suppression override + Spec 35 GFM table mappings
  'src/components/ui/button.tsx:31'                 # text-[0.8rem] — button sm sizing (F2 T01 audit)
  'src/components/chat/output/highlighted-code.tsx:102'  # F4 T07 — github-dark Shiki theme container bg matches the inline pre rendered by codeToHtml
  'src/components/chat/output/highlighted-code.tsx:118'  # F4 T07 — github-dark plain-fallback bg+fg before Shiki tokenises
  # --- typography-sizing retokenise sweep (no-literals gate closure) ---
  # Sans-serif count badges (item/persona/memory counts): tabular-nums, NOT
  # font-mono, NOT uppercase. .type-caption would wrongly swap the family to
  # Geist Mono; .type-ui/.type-body don't express the deliberately smaller
  # 11px badge size. Same exact pattern repeats 3x.
  'src/components/connectors/connectors-manager.tsx:185'  # text-[11px] — category item-count badge
  'src/components/activity/review.tsx:141'                # text-[11px] — section item-count badge (same pattern)
  'src/components/persona/persona-memories-modal.tsx:80'  # text-[11px] — memory-count badge (same pattern)
  # Sans-serif status pills (connector card): title-case translated copy
  # ("Checking…", "Connected", "Coming soon"). Not mono, not uppercase —
  # .type-caption would force both uppercase AND a Geist Mono family swap.
  'src/components/connectors/connector-card.tsx:71'  # text-[11px] — "checking" state pill
  'src/components/connectors/connector-card.tsx:75'  # text-[11px] — "connected" state pill
  'src/components/connectors/connector-card.tsx:80'  # text-[11px] — "coming soon" state pill
  # Sans-serif 13px segmented-toggle / inline label text: sits between
  # .type-ui (14px) and text-xs (12px) as a deliberate toggle-button size;
  # not mono, so .type-code is the wrong family.
  'src/components/artifacts/artifact-gallery.tsx:385'  # text-[13px] — segmented-control button label (default size)
  'src/components/memory/memory-view.tsx:60'           # text-[13px] — mode-toggle button label (same pattern)
  'src/components/activity/review.tsx:457'             # text-[13px] — upcoming-card persona label (same pattern)
  # Sans-serif semibold uppercase status Pill (attention/critical/good/
  # scheduled tags): deliberately bold sans, not mono — .type-caption would
  # swap the family to Geist Mono and drop the font-semibold weight that
  # gives the pill its visual loudness.
  'src/components/activity/review.tsx:86'   # text-[10px] — Pill component (tag/status label)
  # Fraunces-italic editorial pullquotes (persona's own words, voiced line):
  # text-[0.95rem] sits between .type-ui (0.875rem) and .type-heading
  # (1.5rem) at the heading FAMILY, which no .type-* token expresses.
  'src/components/activity/review.tsx:210'  # text-[0.95rem] — WaitingCard voiced pullquote
  'src/components/activity/review.tsx:346'  # text-[0.95rem] — IdeaCard voiced pullquote (same pattern)
  # Sans-serif footer key-hint bar: wraps translated hint copy in the default
  # sans family (child <kbd> elements carry their own font-mono); the
  # wrapper's text-[11px] can't move to a mono .type-* without changing the
  # hint text's family too.
  'src/components/shell/command-palette.tsx:545'  # text-[11px] — footer key-hint bar wrapper
)

# Known-legacy entries: the audit-named text-[0.65rem] uses awaiting close.
# Each entry comes off this list when the owning task lands.
# F2 T16 closed src/components/chat/tier-badge.tsx (→ .type-caption); the
# legacy list shrank from 7 to 6 entries on 2026-06-05.
LEGACY=(
  'src/components/runs/run-status-badge.tsx:21'     # T30 closes
  'src/components/runs/run-view.tsx:49'             # T30 closes
  'src/components/runs/step-card.tsx:47'            # T30 closes
  'src/components/runs/step-card.tsx:51'            # T30 closes
  'src/app/(app)/personas/[id]/page.tsx:136'        # T28 closes
)

SCOPES=(
  'src/components'
  'src/app/(app)'
  'src/app/reference'
  'src/app/page.tsx'
)

# Combine forbidden patterns.
COMBINED="(${FORBIDDEN_COLOUR})|(${FORBIDDEN_TEXT})"

# Find all matches in scope. `|| true` so grep's exit 1 on no-match doesn't
# trip set -e. Then filter out:
#   1. Lines whose content is a comment (`//`, `* ` JSDoc continuation,
#      `*` JSDoc close, or `/*` block-comment open) — comments document the
#      patterns without being violations.
#   2. Test files (*.test.tsx, *.test.ts) — tests assert against the patterns;
#      assertions about the pattern aren't violations.
matches=$(grep -rEn "$COMBINED" "${SCOPES[@]}" \
  --include='*.tsx' --include='*.ts' --include='*.css' \
  --exclude='*.test.tsx' --exclude='*.test.ts' \
  2>/dev/null \
  | grep -vE ':[0-9]+:[[:space:]]*(//|\* |\*$|/\*)' \
  || true)

# Filter matches against allowlist + legacy.
violations=()
if [[ -n "$matches" ]]; then
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    # Extract file:line (everything up to the second `:` — grep -n output is
    # "path:lineno:content"). Note: macOS BSD vs GNU grep agree on this format.
    loc=$(printf '%s\n' "$line" | awk -F':' '{print $1":"$2}')
    skip=0
    for a in "${ALLOWLIST[@]}" "${LEGACY[@]}"; do
      if [[ "$loc" == "$a" ]]; then
        skip=1
        break
      fi
    done
    [[ $skip -eq 1 ]] && continue
    violations+=("$line")
  done <<< "$matches"
fi

if [[ ${#violations[@]} -gt 0 ]]; then
  echo "❌ Found ${#violations[@]} inline design-value literal(s) outside allowlist:"
  echo
  printf '   %s\n' "${violations[@]}"
  echo
  echo "Resolve via F1 tokens (see packages/web/DESIGN.md):"
  echo "  text-[#hex] / text-[oklch(...)]   → text-{color} utilities resolved through @theme"
  echo "  text-[Nrem|em|px]                  → .type-{display|heading|body|ui|caption|code}"
  echo "  Or add to ALLOWLIST/LEGACY in this script with a documented rationale (audit.md)."
  echo
  exit 1
fi

allowlist_count=${#ALLOWLIST[@]}
legacy_count=${#LEGACY[@]}
echo "✅ No-literals gate clean. ${allowlist_count} documented exception(s) + ${legacy_count} legacy entry(ies) allowlisted."
