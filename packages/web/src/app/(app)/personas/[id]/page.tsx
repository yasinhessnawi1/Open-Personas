import { notFound } from "next/navigation";
import { startTask } from "@/app/(app)/runs/actions";
import { PersonaPage } from "@/components/persona/persona-page";
import { mapMcpCatalog } from "@/components/personas/mcp-catalog";
import { fetchMcpConnections } from "@/components/personas/mcp-connections";
import { type ToolSummary, unwrap } from "@/lib/api";
import type { components } from "@/lib/api/schema";
import { serverApi } from "@/lib/api/server";
import { yamlToDoc } from "@/lib/persona-draft";

/**
 * R11-B6 (owner-ruled consolidation) — THE persona page. Detail, edit and the
 * post-authoring preview are one inline-editable, autosaving surface (the kit's
 * `persona-detail.html`): typed-memory stores, constraints, voice, capabilities,
 * model, autonomy+consent and the advanced options (raw YAML + BYO-MCP) all
 * edit in place; the old `/personas/[id]/edit` route redirects here and the
 * "Edit"/"Edit via authoring" buttons are gone.
 *
 * Server side: the same fetch set the edit page used (persona + tools + skills
 * + MCP catalog + per-persona connection status, fail-soft) — one page, one
 * load.
 */
export default async function PersonaDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params; // Next 16: params is async.
  const api = await serverApi();

  const personaRes = await api.GET("/v1/personas/{persona_id}", {
    params: { path: { persona_id: id } },
  });
  if (personaRes.response.status === 404) notFound();
  const detail = await unwrap(personaRes);

  const [tools, skills, mcpCatalog, mcpConnections, artifactsRes] =
    await Promise.all([
      api.GET("/v1/tools").then(unwrap),
      api.GET("/v1/skills").then(unwrap),
      api.GET("/v1/mcp-catalog").then(unwrap),
      fetchMcpConnections(api, id),
      // Files ride the page as an overlay (R11-B6 rider) — first page here,
      // fail-soft to empty (the artifact route is a later-phase addition).
      api.GET("/v1/personas/{persona_id}/artifacts", {
        params: { path: { persona_id: id } },
      }),
    ]);
  const initialArtifacts =
    artifactsRes.response.ok && artifactsRes.data
      ? artifactsRes.data
      : { total: 0, limit: 50, offset: 0, items: [] };

  return (
    <PersonaPage
      personaId={id}
      initialDoc={yamlToDoc(detail.yaml)}
      tools={(tools as ToolSummary[]).map((x) => x.name)}
      skills={(skills as ToolSummary[]).map((x) => x.name)}
      mcpServers={mapMcpCatalog(
        mcpCatalog as components["schemas"]["MCPCatalogServer"][],
      )}
      mcpConnections={mcpConnections}
      initialConsent={detail.consent_to_auto_dispatch ?? null}
      initialAvatarUrl={detail.avatar_url ?? null}
      conversationCount={detail.conversation_count ?? 0}
      tasksRunCount={detail.tasks_run_count ?? 0}
      memoryCount={detail.memory_count ?? 0}
      createdAt={detail.created_at ?? null}
      initialArtifacts={initialArtifacts}
      newTaskAction={startTask}
    />
  );
}
