/**
 * Spec A6 (W5) — the morning Review's typed API access.
 *
 * Hand-typed against `GET /v1/autonomy/review` (the generated `schema.ts` regenerates at merge-back
 * — the W4/A8 precedent). The endpoint serialises the ONE shared `MorningDigest` (B5), the same
 * model C0's morning message renders — so the surface and the message never drift (A6-D-2). Opening
 * the review consumes the deferred chatter exactly once server-side; the client just renders.
 */

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** The durable referent a line deep-links to (A6-D-6): approval → inbox, task → detail. */
export interface DigestRef {
  kind: "approval" | "task";
  id: string;
}

/** One line in a section — persona-voiced; verbatim-safe as text. */
export interface DigestItem {
  persona_id: string;
  title: string;
  detail: string;
  /** The per-item deep-link target (A6-D-6); null when there's no actionable target. */
  ref: DigestRef | null;
  /** A7 "ran because …" provenance — null until W8 wires it (the source now exists post-A7-merge). */
  ran_because: string | null;
}

export interface DigestSection {
  kind: "waiting" | "stuck" | "done" | "initiatives";
  items: DigestItem[];
  overflow: number; // "+N more" beyond the one-minute cap
}

export interface UpcomingItem {
  fire_at: string;
  persona_id: string | null;
  label: string;
}

/** The render-agnostic morning review — the same content the surface and C0 both render (A6-D-2). */
export interface MorningDigest {
  generated_at: string;
  sections: DigestSection[]; // ordered waiting → stuck → done → initiatives; empty omitted
  upcoming: UpcomingItem[];
  total_spent_micros: number;
  persona_names: Record<string, string>;
}

export function fetchReview(
  token: string | null | undefined,
): Promise<MorningDigest> {
  const headers = new Headers({ "Content-Type": "application/json" });
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(`${BASE_URL}/v1/autonomy/review`, { headers }).then((res) => {
    if (!res.ok)
      throw new Error(`GET /v1/autonomy/review failed: ${res.status}`);
    return res.json() as Promise<MorningDigest>;
  });
}
