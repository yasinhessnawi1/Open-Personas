import { getTranslations } from "next-intl/server";

import { ActivityTabs } from "@/components/activity/activity-tabs";
import {
  ApprovalsInbox,
  type PersonaNames,
} from "@/components/approvals/approvals-inbox";
import { PageBody, PageHeader } from "@/components/layout";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";

/**
 * Spec A6 (W4) — the approvals inbox: pending decisions across the caller's tasks.
 *
 * A safety surface, not a summary — each proposal renders the EXACT recorded payload verbatim, and
 * the approve button is reachable only after the proposal is seen (see-then-grant). Every decision
 * goes through the SAME shared resolver the chat twin uses, so a chat-vs-inbox race resolves once
 * and the second surface reflects "already handled" from the durable record (A6-D-3). Personas are
 * fetched server-side for the name map (fail-soft); the pending list is fetched client-side (the
 * A6 endpoints regenerate into the typed client at merge-back — the A8 calendar precedent).
 */
export default async function ApprovalsPage() {
  const t = await getTranslations("approvals");
  const api = await serverApi();
  const personas = await unwrap(await api.GET("/v1/personas")).catch(() => []);
  const personaNames: PersonaNames = Object.fromEntries(
    personas.map((p) => [p.id, p.name]),
  );
  return (
    <PageBody width="narrow">
      <ActivityTabs />
      <PageHeader title={t("title")} subtitle={t("subtitle")} />
      <ApprovalsInbox personaNames={personaNames} />
    </PageBody>
  );
}
