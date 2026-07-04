import { Waypoints } from "lucide-react";
import { getTranslations } from "next-intl/server";
import type { ReactNode } from "react";
import { PageBody, PageHeader } from "@/components/layout";
import { MemoryView } from "@/components/memory/memory-view";
import { EmptyState } from "@/components/patterns/empty-state";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";

/**
 * Spec K5 — "Memory": the interactive knowledge-graph (K5-D-6).
 *
 * The seed window (no focus) is read server-side through the RLS-scoped
 * per-request engine (D-08-1); the route is pure projection of K0 types
 * (K5-D-8). An empty graph renders the invite-not-absence empty state
 * (K5-D-9); a populated graph hands the window to the client canvas
 * (the live-data port of `Memory.html`, K5-R-3).
 */
export default async function MemoryPage() {
  const t = await getTranslations("memory");
  const api = await serverApi();
  const memoryWindow = await unwrap(await api.GET("/v1/memory/graph"));

  let body: ReactNode;
  if (!memoryWindow.available) {
    // No usable knowledge-graph in this deployment (community-on-SQLite / graph
    // off). Honest absence — distinct from "you have no memories yet" — and a
    // defense-in-depth backstop: the nav already hides Memory when unavailable,
    // so this state is reached only by a direct URL (K5).
    body = (
      <EmptyState
        icon={<Waypoints className="size-8" aria-hidden="true" />}
        title={t("unavailable")}
        description={t("unavailableHint")}
      />
    );
  } else if (memoryWindow.nodes.length === 0) {
    body = (
      <EmptyState
        icon={<Waypoints className="size-8" aria-hidden="true" />}
        title={t("empty")}
        description={t("emptyHint")}
      />
    );
  } else {
    body = <MemoryView memoryWindow={memoryWindow} />;
  }

  return (
    <PageBody>
      <PageHeader title={t("title")} subtitle={t("subtitle")} />
      {body}
    </PageBody>
  );
}
