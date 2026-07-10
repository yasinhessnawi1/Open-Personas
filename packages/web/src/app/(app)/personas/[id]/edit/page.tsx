import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";
import { mapMcpCatalog } from "@/components/personas/mcp-catalog";
import { fetchMcpConnections } from "@/components/personas/mcp-connections";
import { PersonaEditor } from "@/components/personas/persona-editor";
import { type ToolSummary, unwrap } from "@/lib/api";
import type { components } from "@/lib/api/schema";
import { serverApi } from "@/lib/api/server";
import { parsePersonaYaml } from "@/lib/persona";
import { savePersona, setConsent } from "@/lib/persona-actions";
import { yamlToDoc } from "@/lib/persona-draft";

export default async function EditPersonaPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params; // Next 16: params is async.
  const t = await getTranslations("author");
  const api = await serverApi();

  const personaRes = await api.GET("/v1/personas/{persona_id}", {
    params: { path: { persona_id: id } },
  });
  if (personaRes.response.status === 404) notFound();
  const detail = await unwrap(personaRes);

  // N6 merge-back: the per-assigned-server connection status is FAIL-SOFT — an
  // error (older api, community without the runtime) yields [] → no badges.
  const [tools, skills, mcpCatalog, mcpConnections] = await Promise.all([
    api.GET("/v1/tools").then(unwrap),
    api.GET("/v1/skills").then(unwrap),
    api.GET("/v1/mcp-catalog").then(unwrap),
    fetchMcpConnections(api, id),
  ]);

  const doc = yamlToDoc(detail.yaml);
  const name = parsePersonaYaml(detail.yaml).name;

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-8 sm:px-6">
      <Link
        href={`/personas/${id}`}
        className="mb-6 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-4" />
        {t("backToPersona")}
      </Link>
      <header className="mb-6">
        <p className="type-caption font-mono uppercase text-muted-foreground">
          {t("editKicker")}
        </p>
        <h1 className="mt-1 font-heading text-2xl font-semibold tracking-tight">
          {t("editTitle", { name })}
        </h1>
      </header>
      <PersonaEditor
        initialDoc={doc}
        tools={(tools as ToolSummary[]).map((x) => x.name)}
        skills={(skills as ToolSummary[]).map((x) => x.name)}
        mcpServers={mapMcpCatalog(
          mcpCatalog as components["schemas"]["MCPCatalogServer"][],
        )}
        mcpConnections={mcpConnections}
        personaId={id}
        onSave={savePersona.bind(null, id)}
        saveLabel={t("save")}
        initialConsent={detail.consent_to_auto_dispatch ?? null}
        initialAvatarUrl={detail.avatar_url ?? null}
        onConsentChange={setConsent.bind(null, id)}
      />
    </div>
  );
}
