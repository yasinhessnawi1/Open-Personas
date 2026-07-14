import { PageBody } from "@/components/layout";
import { AuthorWizard } from "@/components/personas/author-wizard";
import {
  mapMcpCapabilities,
  mapMcpCatalog,
} from "@/components/personas/mcp-catalog";
import { type ToolSummary, unwrap } from "@/lib/api";
import type { components } from "@/lib/api/schema";
import { serverApi } from "@/lib/api/server";

/**
 * Spec F2 T29 — Authoring page (rebuilt presentation).
 *
 * DO NOT TOUCH (per audit.md §authoring.plumbing):
 *   - `serverApi()` server-component fetch + parallel `GET /v1/tools` +
 *     `GET /v1/skills` (the existing draft-wire-up).
 *
 * REPLACED:
 *   - hand-rolled `mx-auto max-w-3xl px-… py-…` → T20 `<PageBody>`;
 *   - inner `<AuthorWizard>` uses its T29-rebuilt presentation (separate file).
 */
export default async function NewPersonaPage() {
  const api = await serverApi();
  const [tools, skills, mcpCatalog, profile] = await Promise.all([
    api.GET("/v1/tools").then(unwrap),
    api.GET("/v1/skills").then(unwrap),
    // Spec 30 T11 — built-in MCP servers for the unified capability section.
    // Spec N7 (D-N7-2): the response is now a wrapper {servers, capabilities}.
    api
      .GET("/v1/mcp-catalog")
      .then(unwrap),
    // Spec M1 (M1-T7) — the sticky per-user model default (T6), pre-selected
    // in the Model section below.
    api
      .GET("/v1/me/profile")
      .then(unwrap),
  ]);
  const catalog = mcpCatalog as components["schemas"]["MCPCatalogResponse"];

  return (
    <PageBody>
      <AuthorWizard
        tools={(tools as ToolSummary[]).map((x) => x.name)}
        skills={(skills as ToolSummary[]).map((x) => x.name)}
        mcpServers={mapMcpCatalog(catalog.servers)}
        mcpCapabilities={mapMcpCapabilities(catalog.capabilities)}
        defaultModel={profile.preferred_model ?? null}
      />
    </PageBody>
  );
}
