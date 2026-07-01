/**
 * Spec S3 — specialities client (the S-track's catalog + consent surface).
 *
 * Fetches the specialities a persona can enable, with THIS persona's server-computed
 * consent state, and records consent for a gated (community/third_party) speciality.
 * The client sends ONLY `{granted}` on consent — the content_hash + tier are
 * server-derived (S3-D-2 forge-prevention); it never supplies them here.
 */

import { createApiClient, type TokenGetter, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

/** A speciality with this persona's consent state (mirrors PersonaSpecialitySummary). */
export type SpecialityEntry = components["schemas"]["PersonaSpecialitySummary"];
type CatalogEntry = components["schemas"]["SpecialitySummary"];

/**
 * The specialities the persona can enable, with consent state.
 *
 * Edit flow (`personaId` present) → `GET /v1/personas/{id}/specialities`, which
 * carries the per-persona `consent_state`. New-persona flow (no `personaId`) →
 * `GET /v1/specialities` (catalog only); a gated skill defaults to `none` so it
 * shows as needing consent on enable, a non-gated one to `not_required`.
 */
export async function fetchSpecialities(
  personaId: string | undefined,
  getToken: TokenGetter,
): Promise<SpecialityEntry[]> {
  const api = createApiClient(getToken);
  if (personaId) {
    return unwrap(
      await api.GET("/v1/personas/{persona_id}/specialities", {
        params: { path: { persona_id: personaId } },
      }),
    );
  }
  const catalog = await unwrap(await api.GET("/v1/specialities"));
  return catalog.map(withDefaultConsentState);
}

function withDefaultConsentState(entry: CatalogEntry): SpecialityEntry {
  return {
    ...entry,
    consent_state: entry.requires_consent ? "none" : "not_required",
  };
}

/**
 * Record consent (grant/revoke) for a gated speciality; returns the updated entry
 * (with the new `consent_state`). The server derives the content_hash + tier.
 */
export async function recordSpecialityConsent(
  personaId: string,
  skillName: string,
  granted: boolean,
  getToken: TokenGetter,
): Promise<SpecialityEntry> {
  const api = createApiClient(getToken);
  return unwrap(
    await api.POST("/v1/personas/{persona_id}/skills/{skill_name}/consent", {
      params: { path: { persona_id: personaId, skill_name: skillName } },
      body: { granted },
    }),
  );
}
