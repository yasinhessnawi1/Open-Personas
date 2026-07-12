import { notFound } from "next/navigation";
import { VoiceCallSurface } from "@/components/voice/voice-call-surface";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";
import { parsePersonaYaml } from "@/lib/persona";

/**
 * Spec V6 B5 / D-V6-4 — the dedicated voice-call route. A full-surface "call
 * with the persona" view bound to the open conversation (voice + text are one
 * thread; the call's transcript persists to the same `conversation_id`). Back
 * navigation returns to the text thread. Mirrors the chat page's conversation +
 * persona fetch so the call surface carries the persona's F1 identity.
 */
export default async function VoiceCallPage({
  params,
}: {
  params: Promise<{ conversationId: string }>;
}) {
  const { conversationId } = await params;
  const api = await serverApi();

  const convRes = await api.GET("/v1/conversations/{conversation_id}", {
    params: { path: { conversation_id: conversationId } },
  });
  if (convRes.response.status === 404) notFound();
  const conv = await unwrap(convRes);

  const personaRes = await api.GET("/v1/personas/{persona_id}", {
    params: { path: { persona_id: conv.persona_id } },
  });
  const persona = personaRes.data
    ? parsePersonaYaml(personaRes.data.yaml)
    : null;

  return (
    // R9-025a rider: the identical unconditional h-[calc(100svh-3.5rem)] bug
    // fixed on the chat page in ee4a314 — the calc only accounts for
    // AppShell's mobile-only sticky header (h-14, md:hidden), so at md+ it
    // sat a fixed 56px short of the viewport. md:h-full overrides at that
    // breakpoint; mobile keeps the header-aware calc.
    <div className="h-[calc(100svh-3.5rem)] md:h-full">
      <VoiceCallSurface
        conversationId={conversationId}
        persona={{
          id: conv.persona_id,
          name: persona?.name ?? conv.title ?? "Persona",
          avatarUrl: personaRes.data?.avatar_url ?? undefined,
          role: persona?.role ?? "",
        }}
      />
    </div>
  );
}
