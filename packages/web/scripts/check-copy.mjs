#!/usr/bin/env node
/**
 * The copy gate: user-facing words are a product surface, so they get a gate.
 *
 * Two failure modes cost us trust with the people we are trying to sign up,
 * and both are mechanical enough to catch here:
 *
 *  1. EM AND EN DASHES. Widely read as machine-written. Forbidden outright in
 *     anything a user sees. (The repo-wide `scripts/check-no-em-dashes.sh`
 *     covers public markdown too; this gate keeps the web catalogue honest on
 *     its own so `pnpm check:copy` is a complete answer for the frontend.)
 *
 *  2. AI-SLOP VOCABULARY. Words that sound like enthusiasm but carry no
 *     information: "seamless", "empower", "unlock", "supercharge". They are a
 *     tell, and worse, each one is a place where a specific fact should have
 *     gone. "Seamless sync" tells the reader nothing; "syncs in under a
 *     second" sells.
 *
 * Scope is `src/i18n/messages/en.json`, the single home of every user-visible
 * string in the app. Gating the catalogue rather than the .tsx files makes the
 * check exact: code comments and identifiers are free to say anything, and a
 * new hardcoded string is already caught by the i18n review it has to pass.
 *
 * Allowlist: a term is exempted per key, never globally, and the entry has to
 * say why. If a word genuinely belongs in one string, that is a fact about
 * that string, not a licence to use it everywhere.
 */
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const CATALOGUE = join(WEB_ROOT, "src/i18n/messages/en.json");

/** Words that almost always replace a concrete fact with a vibe. */
const BANNED_WORDS = [
  "seamless",
  "seamlessly",
  "empower",
  "empowers",
  "empowering",
  "delve",
  "supercharge",
  "supercharged",
  "effortless",
  "effortlessly",
  "unlock",
  "unlocks",
  "unleash",
  "elevate",
  "revolutionary",
  "revolutionize",
  "revolutionise",
  "cutting-edge",
  "game-changing",
  "game changer",
  "best-in-class",
  "world-class",
  "state-of-the-art",
  "next-level",
  "take it to the next level",
  "look no further",
  "dive into",
  "in today's fast-paced",
  "we've got you covered",
  "transform your",
  "boost your",
  "streamline",
];

/** Constructions that read as generated even when every word is ordinary. */
const BANNED_PATTERNS = [
  // Matches the contraction and the spelled-out form: "it's not just",
  // "its not just", "it is not just", "isn't just", "is not just".
  {
    re: /\b(it'?s|it is|isn'?t|is not)\s+(not\s+)?just\b/i,
    why: 'the "not just X, it\'s Y" construction',
  },
  { re: /\bwhether you'?re\b/i, why: 'the "whether you\'re X or Y" hedge' },
  { re: /[—–]/, why: "em or en dash" },
];

/**
 * Per-key exemptions. Shape: "namespace.key": { term, why }.
 * Empty on purpose. Add an entry only with a real justification.
 */
const ALLOWLIST = new Map([]);

/** Flatten the nested catalogue into dotted key paths. */
function* walk(node, path = []) {
  for (const [k, v] of Object.entries(node)) {
    const next = [...path, k];
    if (v && typeof v === "object") yield* walk(v, next);
    else yield [next.join("."), String(v)];
  }
}

const catalogue = JSON.parse(readFileSync(CATALOGUE, "utf8"));
const violations = [];

for (const [key, value] of walk(catalogue)) {
  const exempt = ALLOWLIST.get(key);
  const haystack = value.toLowerCase();

  for (const word of BANNED_WORDS) {
    if (!haystack.includes(word)) continue;
    if (exempt?.term === word) continue;
    violations.push({ key, value, why: `banned word "${word}"` });
  }
  for (const { re, why } of BANNED_PATTERNS) {
    if (!re.test(value)) continue;
    if (exempt?.term === why) continue;
    violations.push({ key, value, why });
  }
}

// One line per distinct (key, reason). A string tripping both "seamless"
// and "seamlessly" is one problem to fix, not two.
const seen = new Set();
const unique = violations.filter((v) => {
  const id = `${v.key}::${v.why}`;
  if (seen.has(id)) return false;
  seen.add(id);
  return true;
});

if (unique.length > 0) {
  console.error(`\nCopy gate failed: ${unique.length} issue(s).\n`);
  for (const v of unique) {
    console.error(`  ${v.key}`);
    console.error(`     ${v.why}`);
    console.error(`     "${v.value}"\n`);
  }
  console.error("Say the specific thing instead. See packages/web/COPY.md.\n");
  process.exit(1);
}

console.log(
  "Copy gate clean. No AI-slop vocabulary or dashes in the catalogue.",
);
