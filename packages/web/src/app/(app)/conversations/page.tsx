import { MessageSquare } from "lucide-react";
import { getTranslations } from "next-intl/server";
import { ConversationFilterStrip } from "@/components/conversations/conversation-filter-strip";
import {
  ConversationList,
  type ConversationListPersona,
} from "@/components/conversations/conversation-list";
import { NewConversationButton } from "@/components/conversations/new-conversation-button";
import { PageBody, PageHeader, Stack } from "@/components/layout";
import { EmptyState } from "@/components/patterns/empty-state";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";

/**
 * Spec F5 T12 — Conversation list page extension.
 *
 * Replaces the scaffold's basic Card-list with the F2-primitive composition
 * per aesthetic-direction.md §1.3:
 *   - Flat reverse-chronological list (NO date grouping — ChatGPT shape
 *     rejected per D-F5-X-conversation-history-organization);
 *   - Hairline `border-b` rows (NOT card boxes);
 *   - `<PersonaAvatar size="sm">` + persona name in identity-coloured accent;
 *   - F2 `<EmptyState>` with inviting copy.
 *
 * T13 adds: persona-filter `.glass-chip` strip; title-only search input;
 * `<DropdownMenu>` per-row delete affordance; "Load older" pagination via
 * TanStack `useInfiniteQuery`.
 */
export default async function ConversationsPage() {
  const t = await getTranslations("conversations");
  const api = await serverApi();
  const [conversations, personas] = await Promise.all([
    api.GET("/v1/conversations").then(unwrap),
    api.GET("/v1/personas").then(unwrap),
  ]);

  const personaMap = new Map<string, ConversationListPersona>(
    personas.map((p) => [
      p.id,
      { id: p.id, name: p.name, avatar_url: p.avatar_url ?? null },
    ]),
  );

  const personaList = Array.from(personaMap.values());

  return (
    <PageBody>
      <PageHeader
        title={t("title")}
        subtitle={t("subtitle")}
        // R9-014 (b): "New message" → reusable persona picker → new chat.
        // Only offered when the owner actually has a persona to message.
        actions={
          personaList.length > 0 ? (
            <NewConversationButton personas={personaList} />
          ) : undefined
        }
      />
      {conversations.length === 0 ? (
        <EmptyState
          icon={<MessageSquare className="size-8" aria-hidden="true" />}
          title={t("empty")}
          description={t("emptyHint")}
        />
      ) : (
        <Stack gap={2}>
          <ConversationFilterStrip personas={personaList} />
          <ConversationList
            conversations={conversations}
            personaById={Object.fromEntries(personaMap)}
          />
        </Stack>
      )}
    </PageBody>
  );
}
