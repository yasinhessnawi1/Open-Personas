import { getTranslations } from "next-intl/server";

import { startTask } from "@/app/(app)/runs/actions";
import { ActivityTabs } from "@/components/activity/activity-tabs";
import { NewTaskCta } from "@/components/activity/new-task-dialog";
import { PageBody, PageHeader } from "@/components/layout";
import { type PersonaNames, TasksList } from "@/components/tasks/tasks-list";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";

/**
 * Spec A6 (W2) — the Tasks list: standing + recent tasks across the caller's personas.
 *
 * The cross-persona state matrix, ordered waiting-first (A6-D-2): waiting-on-you → stuck → active →
 * recent-terminal. The one loud card is a stuck task (A6-D-5) — a red rail + the cause + options
 * (Cancel / resolve → detail); everything else is a calm row. Durable state is truth on load
 * (A6-R-4): the list is fetched, never inferred. Personas are fetched server-side for the name map
 * (fail-soft); the task list is fetched client-side (the A6 endpoints regenerate into the typed
 * client at merge-back — the W4/A8 precedent).
 *
 * R11-B2: the kit's hand-off CTA sits above the list — the create affordance the retired
 * standalone /tasks route used to carry (kit README "/tasks is retired").
 */
export default async function TasksPage() {
  const t = await getTranslations("taskList");
  const api = await serverApi();
  const personas = await unwrap(await api.GET("/v1/personas")).catch(() => []);
  const personaNames: PersonaNames = Object.fromEntries(
    personas.map((p) => [p.id, p.name]),
  );
  const personaOptions = personas.map((p) => ({
    id: p.id,
    name: p.name,
    avatar_url: p.avatar_url,
  }));
  return (
    <PageBody width="narrow">
      <ActivityTabs />
      <PageHeader title={t("title")} subtitle={t("subtitle")} />
      <NewTaskCta personas={personaOptions} action={startTask} />
      <TasksList personaNames={personaNames} />
    </PageBody>
  );
}
