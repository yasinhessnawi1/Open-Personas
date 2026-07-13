/**
 * Spec R11 (B2, D-R11-5) — the Activity quick-access ranking.
 *
 * The Home dashboard's "jump back in" recency logic, extracted when Home
 * retired: rank personas by most-recent USE. `conversations` arrives already
 * sorted `updated_at DESC` from `/v1/conversations`, so the FIRST appearance
 * of each persona_id is that persona's most-recent activity; personas never
 * talked to fill the tail, most-recently-created first. Derived entirely from
 * existing data — no favorites field, no schema change.
 */

export interface RankablePersona {
  readonly id: string;
  readonly created_at: string;
}

export interface RankableConversation {
  readonly persona_id: string;
}

export function rankPersonasByRecentUse<P extends RankablePersona>(
  personas: readonly P[],
  conversationsNewestFirst: readonly RankableConversation[],
  limit: number,
): P[] {
  const byId = new Map(personas.map((p) => [p.id, p]));
  const seen = new Set<string>();
  const used: P[] = [];
  for (const c of conversationsNewestFirst) {
    const p = byId.get(c.persona_id);
    if (p && !seen.has(p.id)) {
      seen.add(p.id);
      used.push(p);
      if (used.length >= limit) return used;
    }
  }
  const unused = personas
    .filter((p) => !seen.has(p.id))
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  return [...used, ...unused].slice(0, limit);
}
