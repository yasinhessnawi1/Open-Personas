import { ChevronLeft } from "lucide-react";
import Link from "next/link";
import { getTranslations } from "next-intl/server";

import { PageBody, PageHeader } from "@/components/layout";
import { TaskDetail } from "@/components/tasks/task-detail";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";

/**
 * Spec A6 (W3) — the task detail: contract + grants, budget + ledger, checkpoints, terminal report,
 * and the in-context controls (A6-D-1).
 *
 * Reads render the durable truth (A6-R-4): the detail is fetched client-side and refetched after
 * every command, so a mutation reflects the durable post-state — never the optimistic guess. Controls
 * ride the REAL doors: pause/resume/cancel/budget-extend (B2), the persona's initiative dial (B4),
 * schedule-edit through the A8 CAS door, and the drill into `/runs` for the leg viewer. Checkpoints
 * render as progress + next-step only (A6-D-4) — open questions on expand, never raw transcripts.
 */
export default async function TaskDetailPage({
  params,
}: {
  params: Promise<{ taskId: string }>;
}) {
  const { taskId } = await params;
  const t = await getTranslations("taskDetail");
  const api = await serverApi();
  const personas = await unwrap(await api.GET("/v1/personas")).catch(() => []);
  const personaNames: Record<string, string> = Object.fromEntries(
    personas.map((p) => [p.id, p.name]),
  );
  return (
    <PageBody width="narrow">
      <Link
        href="/tasks"
        className="mb-2 inline-flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ChevronLeft className="size-4" />
        {t("back")}
      </Link>
      <PageHeader title={t("title")} />
      <TaskDetail taskId={taskId} personaNames={personaNames} />
    </PageBody>
  );
}
