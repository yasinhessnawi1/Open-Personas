"use client";

import { useTranslations } from "next-intl";
import { startChat } from "@/app/actions";
import type { AvatarPersona } from "@/components/persona/persona-avatar";
import { PersonaPicker } from "@/components/persona/persona-picker";

/**
 * R9-014 (b) — "New message" affordance for the conversations page.
 *
 * Thin conversations-specific wrapper around the reusable `<PersonaPicker>`:
 * choosing a persona reuses the `startChat` server action (POST
 * /v1/personas/{id}/conversations → redirect to /chat/{id}) — the same
 * create-and-open flow as the persona library card. The picker itself stays
 * presentation-agnostic; only THIS caller ties selection to conversation
 * creation.
 */
export function NewConversationButton({
  personas,
}: {
  personas: readonly AvatarPersona[];
}) {
  const t = useTranslations("conversations");
  return (
    <PersonaPicker
      personas={personas}
      label={t("newMessage")}
      onSelect={(personaId) => {
        // Server action: creates the conversation then redirect()s to /chat/{id}.
        void startChat(personaId);
      }}
    />
  );
}
