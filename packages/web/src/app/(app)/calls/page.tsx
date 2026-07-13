import { Phone } from "lucide-react";
import { getTranslations } from "next-intl/server";
import { CallFilterStrip } from "@/components/calls/call-filter-strip";
import {
  CallHistoryList,
  type CallHistoryPersona,
} from "@/components/calls/call-history-list";
import { NewCallButton } from "@/components/calls/new-call-button";
import { PageBody, PageHeader, Stack } from "@/components/layout";
import { EmptyState } from "@/components/patterns/empty-state";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";

/**
 * Spec V9 / R9-028 — the voice-call history page.
 *
 * A flat reverse-chronological list of the caller's calls (`GET /v1/calls`,
 * newest-first, owner-scoped). Each row opens the call's saved transcript
 * (`/chat/{conversation_id}` — the spoken turns persist as conversation
 * messages, V9-D-1/D-2, so the existing chat page renders them). Personal-model:
 * a user sees only their own calls (RLS).
 *
 * R9-028 brings the R9-014 conversations-page treatment: (a) "New call" via
 * the reusable persona picker — starts a call through the existing
 * origination flow (no new one); (b) titles from the R9-020 title_refresh
 * voice leg render per row; (c) the persona-filter strip + title search R9-014
 * already fixed, built with the fix in place from day one.
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
  const personaList = Object.values(personaById);

  return (
    <PageBody>
      <PageHeader
        title={t("title")}
        subtitle={t("subtitle")}
        // R9-028 (a): "New call" — only offered when the owner has a persona
        // to call, mirroring the conversations page's "New message" gate.
        actions={
          personas.length > 0 ? (
            <NewCallButton
              personas={personas.map((p) => ({
                id: p.id,
                name: p.name,
                avatar_url: p.avatar_url ?? null,
                role: p.role,
              }))}
            />
          ) : undefined
        }
      />
      {calls.length === 0 ? (
        <EmptyState
          icon={<Phone className="size-8" aria-hidden="true" />}
          title={t("empty")}
          description={t("emptyHint")}
        />
      ) : (
        <Stack gap={2}>
          <CallFilterStrip personas={personaList} />
          <CallHistoryList
            calls={calls.map((c) => ({
              call_id: c.call_id,
              conversation_id: c.conversation_id,
              persona_id: c.persona_id,
              title: c.title,
              started_at: c.started_at,
              duration_s: c.duration_s ?? null,
            }))}
            personaById={personaById}
          />
        </Stack>
      )}
    </PageBody>
  );
}
