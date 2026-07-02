#!/usr/bin/env node
/**
 * Spec P6 (P6-D-15) — the toast-façade gate: AC#5's invariant, made structural.
 *
 * Spec 35's AC#5 required every user-facing message to route through the one
 * sanctioned notification surface. P6 (Deliverable 4, Path B) evolved that: a
 * cross-device feed can't go through `useNotify()` (which persists to
 * localStorage), so server-authored notifications surface through
 * `ServerNotificationsProvider`. The invariant AC#5 protects is unchanged — ONE
 * bell, sanctioned façades only, zero direct sonner, zero native dialogs — so
 * this gate enumerates exactly the sanctioned importers and fails a third,
 * unsanctioned toast pathway added later (a ruling, not a reinterpretation):
 *
 *  1. NO DIRECT SONNER — only the toast façade (`components/patterns/toast.tsx`)
 *     may `import ... from "sonner"`. Everything else uses the façade.
 *  2. SANCTIONED TOAST CALLERS — only the two feed surfaces
 *     (`notification-provider` = useNotify, `server-notifications-provider`) may
 *     import the imperative `toast` from the façade. (The shell imports
 *     `ToastProvider`, the mount — not a message pathway — which is allowed.)
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SRC = join(WEB_ROOT, "src");

/** Only these files may import from "sonner" directly. */
const SONNER_ALLOW = new Set([
  "src/components/patterns/toast.tsx", // the façade
  "src/app/scratch/toasts/page.tsx", // dev scratch preview (non-production)
]);
/** Only these files may import the imperative `toast` from the façade. */
const TOAST_ALLOW = new Set([
  "src/components/providers/notification-provider.tsx", // useNotify — client feed
  "src/components/providers/server-notifications-provider.tsx", // server feed
]);

const SONNER_RE = /(?:from|import)\s+["']sonner["']/;
// A named import that pulls in the imperative `toast` from the façade. `\btoast\b`
// is case-sensitive, so it never matches `ToastProvider`/`Toaster`.
const TOAST_IMPORT_RE =
  /import\s*(?:type\s*)?{[^}]*\btoast\b[^}]*}\s*from\s*["']@\/components\/patterns\/toast["']/;

const isTest = (p) => /\.test\.|\.spec\.|__tests__|__mocks__/.test(p);

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.tsx?$/.test(p)) out.push(p);
  }
  return out;
}

const files = walk(SRC).filter((p) => !isTest(p));
const sonnerOffenders = [];
const toastOffenders = [];

for (const p of files) {
  const rel = relative(WEB_ROOT, p);
  const src = readFileSync(p, "utf8");
  if (SONNER_RE.test(src) && !SONNER_ALLOW.has(rel)) sonnerOffenders.push(rel);
  if (TOAST_IMPORT_RE.test(src) && !TOAST_ALLOW.has(rel))
    toastOffenders.push(rel);
}

let failed = false;
if (sonnerOffenders.length) {
  failed = true;
  console.error(
    "❌ direct `sonner` import outside the toast façade (route through @/components/patterns/toast):",
  );
  for (const f of sonnerOffenders) console.error(`   - ${f}`);
}
if (toastOffenders.length) {
  failed = true;
  console.error(
    "❌ imperative `toast` imported outside the sanctioned feed surfaces " +
      "(useNotify / ServerNotificationsProvider — the two bell feeds):",
  );
  for (const f of toastOffenders) console.error(`   - ${f}`);
}
if (failed) {
  console.error(
    "\nAC#5 (P6-D-15): every message flows through a sanctioned façade feeding the one bell.",
  );
  process.exit(1);
}
console.log(
  "✅ toast-façade gate clean: sonner is façade-only; the imperative toast is confined to the two sanctioned feed surfaces.",
);
