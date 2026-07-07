import type { Client } from "openapi-fetch";
import type { paths } from "@/lib/api/schema";
import {
  type McpConnectionStatus,
  toConnectionStatus,
} from "./mcp-connection-label";

/**
 * N6 merge-back — fetch a persona's per-assigned-server connection status
 * (`GET /v1/personas/{id}/mcp-connections`) for the badge surfaces, FAIL-SOFT:
 * any error (older api without the route → 404/undefined data, community
 * edition without the runtime, network failure) resolves to `[]`, which every
 * consumer renders as "no badges" — the connection signal is an enrichment and
 * must never break the persona pages. Shared by the profile + edit pages so the
 * fail-soft semantics have one home (the `mapMcpCatalog` pattern).
 */
export async function fetchMcpConnections(
  api: Client<paths>,
  personaId: string,
): Promise<McpConnectionStatus[]> {
  try {
    const res = await api.GET("/v1/personas/{persona_id}/mcp-connections", {
      params: { path: { persona_id: personaId } },
    });
    return (res.data ?? []).map(toConnectionStatus);
  } catch {
    return [];
  }
}
