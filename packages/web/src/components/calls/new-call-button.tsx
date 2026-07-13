"use client";

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";
import type { AvatarPersona } from "@/components/persona/persona-avatar";
import { PersonaPicker } from "@/components/persona/persona-picker";
import { useApi } from "@/lib/api/use-api";
import {
  type CallTarget,
  useCallSession,
} from "@/lib/voice/call-session-context";

export interface NewCallPersona extends AvatarPersona {
  role: string;
}

/**
 * R9-028 (a) — "New call" affordance for the `/calls` page.
 *
 * The R9-014 reusable `<PersonaPicker>` (avatar + name), wired to the SAME
 * call-origination flow `persona-library-card.tsx`'s `handleCall` already
 * uses (V7 D-V7-4/T4b — no new origination path): mint an `origin='call'`
 * conversation (the V9 birth-marker), then hand it to the hoisted call
 * session via `requestCall` — which either starts the call directly or opens
 * the end-and-switch confirm when a different call is already live.
 */
export function NewCallButton({
  personas,
}: {
  personas: readonly NewCallPersona[];
}) {
  const t = useTranslations("calls");
  const router = useRouter();
  const api = useApi();
  const { requestCall } = useCallSession();
  const [busy, setBusy] = useState(false);

  async function handleSelect(personaId: string): Promise<void> {
    if (busy) return;
    const persona = personas.find((p) => p.id === personaId);
    if (!persona) return;
    setBusy(true);
    try {
      const conv = await api.POST("/v1/personas/{persona_id}/conversations", {
        params: { path: { persona_id: personaId } },
        body: { title: "", origin: "call" },
      });
      if (!conv.data) return;
      const target: CallTarget = {
        personaId,
        conversationId: conv.data.id,
        personaName: persona.name,
        personaAvatarUrl: persona.avatar_url ?? undefined,
        personaRole: persona.role,
      };
      // "switch" opens the end-and-switch confirm, which navigates on confirm
      // — don't navigate here, or we'd land on the new call before it's live.
      if (requestCall(target) !== "switch") {
        router.push(`/chat/${conv.data.id}/voice`);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <PersonaPicker
      personas={personas}
      label={t("newCall")}
      onSelect={(personaId) => void handleSelect(personaId)}
    />
  );
}
