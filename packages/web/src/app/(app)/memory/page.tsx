import { getTranslations } from "next-intl/server";
import { PageBody, PageHeader } from "@/components/layout";
import { MemoryView } from "@/components/memory/memory-view";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";

/**
 * Spec K5 — "Memory": the interactive knowledge-graph (K5-D-6).
 * Spec K11 (T4, D-K11-5) — plus the Episodic tab: a second, independent graph
 * (gists + raw members), per-persona.
 *
 * The seed window (no focus) and the owner's personas (the Episodic tab's
 * picker) are read server-side through the RLS-scoped per-request engine
 * (D-08-1); the route is pure projection of K0 types (K5-D-8) plus
 * `PersonaSummary`. The concept graph's availability/empty placeholders now
 * live inside `<MemoryView>` (K5-D-9's invite-not-absence copy, unchanged) —
 * the toggle itself always renders, since episodic availability is its own,
 * independent signal (D-K11-5) rather than mirroring the concept graph's.
 */
export default async function MemoryPage() {
  const t = await getTranslations("memory");
  const api = await serverApi();
  const [memoryWindow, personas] = await Promise.all([
    api.GET("/v1/memory/graph").then(unwrap),
    api
      .GET("/v1/personas")
      .then(unwrap)
      .catch(() => []),
  ]);

  return (
    <PageBody>
      <PageHeader title={t("title")} subtitle={t("subtitle")} />
      <MemoryView
        memoryWindow={memoryWindow}
        personas={personas.map((p) => ({
          id: p.id,
          name: p.name,
          avatar_url: p.avatar_url,
        }))}
      />
    </PageBody>
  );
}
