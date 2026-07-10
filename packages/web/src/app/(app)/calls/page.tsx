import { Phone } from "lucide-react";
import { getTranslations } from "next-intl/server";
import {
  CallHistoryList,
  type CallHistoryPersona,
} from "@/components/calls/call-history-list";
import { PageBody, PageHeader } from "@/components/layout";
import { EmptyState } from "@/components/patterns/empty-state";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";

/**
 * Spec V9 — the voice-call history page.
 *
 * A flat reverse-chronological list of the caller's calls (`GET /v1/calls`,
 * newest-first, owner-scoped). Each row opens the call's saved transcript
 * (`/chat/{conversation_id}` — the spoken turns persist as conversation
 * messages, V9-D-1/D-2, so the existing chat page renders them). Personal-model:
 * a user sees only their own calls (RLS).
 */
const CALL_HISTORY_LIMIT = 100;

export default async function CallsPage() {
  const t = await getTranslations("calls");
  const api = await serverApi();
  const [calls, personas] = await Promise.all([
    api
      .GET("/v1/calls", {
        params: { query: { limit: CALL_HISTORY_LIMIT, offset: 0 } },
      })
      .then(unwrap),
    api.GET("/v1/personas").then(unwrap),
  ]);

  const personaById: Record<string, CallHistoryPersona> = Object.fromEntries(
    personas.map((p) => [
      p.id,
      { id: p.id, name: p.name, avatar_url: p.avatar_url ?? null },
    ]),
  );

  return (
    <PageBody>
      <PageHeader title={t("title")} subtitle={t("subtitle")} />
      {calls.length === 0 ? (
        <EmptyState
          icon={<Phone className="size-8" aria-hidden="true" />}
          title={t("empty")}
          description={t("emptyHint")}
        />
      ) : (
        <CallHistoryList
          calls={calls.map((c) => ({
            call_id: c.call_id,
            conversation_id: c.conversation_id,
            persona_id: c.persona_id,
            started_at: c.started_at,
            duration_s: c.duration_s ?? null,
          }))}
          personaById={personaById}
        />
      )}
    </PageBody>
  );
}
