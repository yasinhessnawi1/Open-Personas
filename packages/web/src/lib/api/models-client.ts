/**
 * Spec M1 (T5 + T7) — the model catalog's hand-typed client.
 *
 * ``GET /v1/models`` (T5) serves the curated shortlist (default,
 * ``scope=recommended``) or the full browse-all catalog (``scope=all``), each
 * entry priced in USD per 1M tokens. Hand-typed against the T5 route per the
 * `schedule-client.ts` precedent — a plain authed fetch, Bearer token in (the
 * generated `schema.ts` regenerates separately and now also carries this
 * route; this thin client is the one T7's self-fetching Model section calls).
 *
 * FAIL-OPEN AT THE ROUTE (T5): a catalog fetch failure still returns 200 with
 * an empty/partial `models` list + `stale:true` — never a 500. This client
 * still THROWS on a genuine transport/auth failure (network down, non-2xx);
 * the caller (the persona editor's self-fetching Model section) catches that
 * and degrades to an empty list — the picker then shows "Use tier default"
 * only, never an error state (M1-T7).
 */

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** One model in the catalog — T5's `ModelOut` wire shape, consumed verbatim. */
export interface ModelOption {
  /** The canonical OpenRouter model id (e.g. "anthropic/claude-sonnet-4.6"). */
  id: string;
  /** Human display name. */
  label: string;
  /** The id's provider prefix (e.g. "anthropic"). */
  provider: string;
  /** USD per 1,000,000 INPUT tokens. */
  input_price_per_1m: number;
  /** USD per 1,000,000 OUTPUT tokens. */
  output_price_per_1m: number;
  /** Maximum context window, tokens. */
  context_length: number;
  /** Whether this model advertises native tool calling. */
  tools_supported: boolean;
  /** Whether this id is in the curated shortlist. */
  recommended: boolean;
}

interface ModelsResponse {
  models: ModelOption[];
  source: "openrouter";
  stale: boolean;
}

/**
 * `GET /v1/models?scope=…` — the curated shortlist or the full catalog, priced.
 *
 * Returns just the `models` array (the envelope's `source`/`stale` are not
 * surfaced separately — a stale/partial 200 and a transport failure both
 * degrade the SAME way for the picker: fewer/no priced options, "Use tier
 * default" always still works).
 */
export async function getModels(
  token: string | null | undefined,
  scope: "recommended" | "all" = "recommended",
): Promise<ModelOption[]> {
  const headers = new Headers({ "Content-Type": "application/json" });
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${BASE_URL}/v1/models?scope=${scope}`, {
    headers,
  });
  if (!res.ok) {
    throw new Error(`GET /v1/models failed: ${res.status}`);
  }
  const data = (await res.json()) as ModelsResponse;
  return data.models;
}
