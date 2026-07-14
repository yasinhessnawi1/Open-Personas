import { Sparkles } from "lucide-react";
import Link from "next/link";
import { getTranslations } from "next-intl/server";
import { PageBody, PageHeader } from "@/components/layout";
import { EmptyState } from "@/components/patterns/empty-state";
import { PersonaLibrary } from "@/components/persona/persona-library";
import { buttonVariants } from "@/components/ui/button";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";
import { cn } from "@/lib/utils";

/**
 * Spec F2 T27 — Persona list screen (rebuilt).
 *
 * Strangler-fig replace of the scaffold's `/(app)/personas/page.tsx`.
 *
 * DO NOT TOUCH (per audit.md §personas-list.plumbing):
 *   - `serverApi()` + `GET /v1/personas` via the generated openapi-fetch client;
 *   - the Clerk server-token wiring inside `serverApi()`;
 *   - the Link-based routing to `/personas/new` + `/personas/{id}`.
 *
 * REPLACED (presentation only):
 *   - hand-rolled `<header>` markup → T20 `<PageHeader title subtitle actions>`;
 *   - hand-rolled `mx-auto max-w-4xl px-… py-…` → T20 `<PageBody>` (centralises
 *     the width-vs-padding contract; lets us shift the whole shell at once);
 *   - hand-rolled `grid grid-cols-2` → T20 `<Grid cols={{base:1,sm:2,lg:3}}>`
 *     (matches the F1 `/reference/personas` 3-up at lg+);
 *   - scaffold `<PersonaCard>` (with the D-F1-5 violation: uniform
 *     `bg-primary/10` avatar fill) → F2 T14 `<PersonaCard>` (composes
 *     `<PersonaAvatar>` so the fill is per-persona derived);
 *   - inline empty-state markup → T22 `<EmptyState icon title description action>`.
 *
 * F1 visual target: `/reference/personas`. Astrid + Kai + Maren read as three
 * distinct individuals in the live list (the §4 individuality-within-coherence
 * proof made operational).
 */
export default async function PersonasPage() {
  const t = await getTranslations("personas");
  const api = await serverApi();
  const personas = await unwrap(await api.GET("/v1/personas"));

  return (
    <PageBody width="wide">
      <PageHeader
        title={t("title")}
        subtitle={t("subtitle")}
        actions={
          <Link href="/personas/new" className={cn(buttonVariants(), "gap-2")}>
            <Sparkles className="size-4" aria-hidden="true" />
            {t("create")}
          </Link>
        }
      />

      {personas.length === 0 ? (
        <EmptyState
          icon={<Sparkles className="size-8" aria-hidden="true" />}
          title={t("empty")}
          description={t("emptyHint")}
          action={
            <Link
              href="/personas/new"
              className={cn(buttonVariants(), "gap-2")}
            >
              <Sparkles className="size-4" aria-hidden="true" />
              {t("create")}
            </Link>
          }
        />
      ) : (
        // R11-B6: search + count + create-tile grid (kit personas.html).
        <PersonaLibrary personas={personas} />
      )}
    </PageBody>
  );
}
