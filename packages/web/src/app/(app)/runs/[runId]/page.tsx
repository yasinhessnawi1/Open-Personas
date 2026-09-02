import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";
import { PageBody } from "@/components/layout";
import { PersonaAvatar } from "@/components/persona/persona-avatar";
import { RunView } from "@/components/runs/run-view";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";
import { parsePersonaYaml } from "@/lib/persona";

/**
 * Spec F2 T30 — Run viewer page (rebuilt presentation).
 *
 * DO NOT TOUCH (per audit.md §runs.plumbing):
 *   - `serverApi()` GET `/v1/runs/{run_id}` + GET `/v1/personas/{persona_id}`;
 *   - `parsePersonaYaml` for the brief;
 *   - `notFound()` on 404 (Next 16 async-params contract);
 *   - `<RunView>` client component composing `useRun` + SSE consumption.
 *
 * REPLACED:
 *   - hand-rolled `<Avatar>` with `bg-primary/10` fallback (D-F1-5 violation,
 *     scaffold line 49) → T06 `<PersonaAvatar size="lg">` (per-persona
 *     identity-coloured fill);
 *   - hand-rolled `mx-auto max-w-3xl` wrapper → T20 `<PageBody>`;
 *   - byline `font-mono text-xs tracking-wide uppercase` → `.type-caption font-mono uppercase`;
 *   - title `font-heading text-2xl ... tracking-tight` → `.type-heading`.
 */
export default async function RunPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  const t = await getTranslations("runs");
  const ta = await getTranslations("app");
  const api = await serverApi();

  const runRes = await api.GET("/v1/runs/{run_id}", {
    params: { path: { run_id: runId } },
  });
  if (runRes.response.status === 404) notFound();
  const run = await unwrap(runRes);

  const personaRes = await api.GET("/v1/personas/{persona_id}", {
    params: { path: { persona_id: run.persona_id } },
  });
  const persona = personaRes.data
    ? parsePersonaYaml(personaRes.data.yaml)
    : null;
  const personaName = persona?.name ?? ta("personaFallback");

  const headerPersona = personaRes.data
    ? {
        id: personaRes.data.id,
        name: personaName,
        avatar_url: personaRes.data.avatar_url ?? null,
      }
    : null;

  return (
    <PageBody>
      <Link
        href={`/personas/${run.persona_id}`}
        className="type-ui mb-6 inline-flex items-center gap-1.5 text-muted-foreground hover:text-foreground"
        data-slot="back-link"
      >
        <ArrowLeft className="size-4" aria-hidden="true" />
        {t("backToPersona")}
      </Link>

      <header
        className="mb-8 flex items-start gap-4"
        data-slot="run-page-header"
      >
        {headerPersona ? (
          <PersonaAvatar persona={headerPersona} size="lg" />
        ) : null}
        <div className="min-w-0 flex-1">
          <p
            className="type-caption font-mono text-muted-foreground uppercase"
            data-slot="run-byline"
          >
            {t("runByline", { name: personaName })}
          </p>
          <h1 className="type-heading mt-1" data-slot="run-task-title">
            {run.task}
          </h1>
        </div>
      </header>

      <RunView runId={runId} initial={run} />
    </PageBody>
  );
}
