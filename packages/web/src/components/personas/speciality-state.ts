import type { components } from "@/lib/api/schema";

/**
 * Spec S3 (S3-D-4) — the pure speciality-state model.
 *
 * Derives the single presented state of a speciality (skill) for a persona from
 * already-loaded data: the catalog entry (with its trust tier + the server-computed
 * consent_state) + the persona's declared `skills:` list. No I/O, no React — the
 * S-track analog of N3's `deriveAppState`, reused by the specialities chooser.
 *
 * The four states:
 *   - `enabled`      — declared in `skills:` and either not consent-gated
 *                      (builtin/vetted) OR consented at the current body hash.
 *   - `needs-consent`— a gated tier (community/third_party) that is declared but has
 *                      no VALID current-hash consent (never consented, or `stale`
 *                      because the body changed — S1-D-5 re-gate). Declared but not
 *                      actually active → surfaced OVER `enabled`, never as healthy.
 *   - `unavailable`  — declared then dropped from the catalog (an S2 sync removed it);
 *                      a graceful tombstone, never a broken card.
 *   - `available`    — the floor: not declared.
 *
 * Precedence (FIXED): `unavailable > needs-consent > enabled > available`.
 *
 * Enablement is the `skills:` DECLARATION (client-derived from the edited draft, like
 * N3's `mcp:<name>` in `tools`); consent is the separate server-side record the
 * `consent_state` reflects. `unavailable` is passed in (the declared-but-dropped set
 * the chooser computes from the draft ∩ catalog), mirroring N3's `unavailableMcpServers`.
 */
export type SpecialityEntry = components["schemas"]["PersonaSpecialitySummary"];

export type SpecialityState =
  | "available"
  | "needs-consent"
  | "enabled"
  | "unavailable";

/** Whether the persona declares this speciality (its name is in the `skills:` list). */
export function isSpecialityEnabled(
  name: string,
  declaredSkills: readonly string[],
): boolean {
  return declaredSkills.includes(name);
}

/**
 * Whether this speciality is consent-gated AND not currently consented at the
 * server's current body hash (`none` or `stale`). A gated tier whose consent is
 * missing or stale is not injectable (default-deny / re-gate) — the UI must reflect
 * that rather than show it as consented.
 */
export function needsConsent(entry: SpecialityEntry): boolean {
  return entry.requires_consent && entry.consent_state !== "granted";
}

/** Derive the single presented {@link SpecialityState} for a speciality on a persona. */
export function deriveSpecialityState(
  entry: SpecialityEntry,
  declaredSkills: readonly string[],
  unavailableSpecialities: readonly string[] = [],
): SpecialityState {
  // Precedence: unavailable > needs-consent > enabled > available.
  if (unavailableSpecialities.includes(entry.name)) return "unavailable";
  const declared = isSpecialityEnabled(entry.name, declaredSkills);
  if (needsConsent(entry)) {
    // Declared but not validly consented → declared-but-inactive → needs-consent.
    // Not declared → still just available (enabling runs the consent flow first).
    return declared ? "needs-consent" : "available";
  }
  return declared ? "enabled" : "available";
}
