import Link from "next/link";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";
import { ChatPresenceOrb } from "@/components/chat/chat-presence-orb";
import { ChatRemembersButton } from "@/components/chat/chat-remembers-button";
import { ChatRightPanelGroup } from "@/components/chat/chat-right-panel-group";
import { ChatWindow } from "@/components/chat/chat-window";
import type { ChatMessageView } from "@/components/chat/message-element";
import { ActiveCallIndicator } from "@/components/voice/active-call-indicator";
import { CallControl } from "@/components/voice/call-control";
import { CallRecap } from "@/components/voice/call-recap";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";
import { persistedToView } from "@/lib/chat/reduce-chat-event";
import { parsePersonaYaml } from "@/lib/persona";
import { personaIdentityStyle } from "@/lib/persona-identity";

/**
 * T26: rebuilt chat screen. Header swaps the scaffold's <Avatar> +
 * `bg-primary/10` uniform fallback for the D-F1-5 PersonaIdentityHeader
 * (avatar in the derived identity colour + 1px identity-coloured underline
 * beneath the persona name + the optional constraint cue). Chat body
 * delegates to <ChatWindow>, which now renders <MessageElement> with the
 * D-F2-15 interleaved tool layout.
 *
 * DO NOT TOUCH: the serverApi() conversation + persona fetches, the
 * notFound() on 404, parsePersonaYaml, the conversation-message → view
 * mapping. Audit §chat.plumbing covers the strangler-fig inventory.
 *
 * R9-026 REOPEN: the root's height was a flat `h-[calc(100svh-3.5rem)]` —
 * correct only below `md`, where AppShell's mobile-only sticky header
 * (`h-14` = 3.5rem, `md:hidden`) actually consumes that space above
 * `<main>`. At `md` and up that header is hidden (0px), but the calc kept
 * subtracting 3.5rem anyway, so this root sat a fixed 56px short of
 * `<main>`'s true (flex-grown, viewport-filling) height on desktop — a
 * dead band below the composer. `<main className="flex flex-1 flex-col">`
 * (components/shell/app-shell.tsx) DOES fill the viewport on desktop
 * (verified: Sidebar's `md:h-svh` gives the outer shell row a definite
 * cross size that stretches through to `<main>`), so `md:h-full` here
 * correctly resolves against it. Mobile keeps the header-aware calc.
 */
export default async function ChatPage({
  params,
}: {
  params: Promise<{ conversationId: string }>;
}) {
  const { conversationId } = await params;
  const tc = await getTranslations("chat");
  const ta = await getTranslations("app");
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
  const name = persona?.name ?? conv.title ?? ta("personaFallback");
  const role = persona?.role ?? "";
  const constraint = persona?.constraints[0];
  // Spec 35: the real shared-memory count for the header's "remembers N" line.
  const remembers = personaRes.data?.conversation_count ?? 0;

  // The persona shape PersonaIdentityHeader + MessageElement consume.
  // id drives the deterministic identity-colour derivation; avatar_url
  // overrides the initials-mark when present.
  const personaForDisplay = {
    id: conv.persona_id,
    name,
    avatar_url: personaRes.data?.avatar_url ?? undefined,
    role,
    constraint,
  };

  // Spec P3 (P3-D-4): reconstruct the interleaved view from the persisted ordered
  // log via the SHARED `persistedToView` — the single mapper that replaced the two
  // divergent text-only maps (this initial-load builder + `reload()` in use-chat).
  // A turn with tool cards / a chart / a file now reappears interleaved on initial
  // page load, conversation-switch, and refresh; a legacy / NULL-`events` row
  // degrades to the byte-exact text-only render (carrying `tier_used`, Spec 35).
  const initialMessages: ChatMessageView[] = conv.messages.map((m) =>
    persistedToView(m),
  );

  return (
    <div className="flex h-[calc(100svh-3.5rem)] flex-col md:h-full">
      {/* Spec 35: chat header on the editorial .v-chat__head. The avatar + name
          route to the persona detail; the presence avatar (#3) pulses while
          live; the role line surfaces the real shared-memory count. */}
      <div
        className="v-chat__head"
        style={personaIdentityStyle(personaForDisplay)}
      >
        <Link
          href={`/personas/${conv.persona_id}`}
          aria-label={tc("openPersona", { name })}
          className="flex min-w-0 flex-1 items-center gap-3.5 outline-none"
          data-slot="chat-header-persona"
        >
          <ChatPresenceOrb persona={personaForDisplay} />
          <div className="v-chat__head-meta">
            <div className="v-chat__name">
              <span className="v-id-underline">{name}</span>
            </div>
            <div className="v-chat__role">
              {role}
              {remembers > 0 ? (
                <>
                  {" · "}
                  {/* K11-T5 (D-K11-7c): remembers N is a button now — opens the
                      persona-scoped episodic manager. */}
                  <ChatRemembersButton
                    persona={personaForDisplay}
                    count={remembers}
                  />
                </>
              ) : null}
            </div>
          </div>
        </Link>
        {/* V7 D-V7-5: live cue + one-tap return, only when this persona is on a call. */}
        <ActiveCallIndicator personaId={conv.persona_id} />
        {/* Spec 35 — conversation Files viewer (next to Call, per the v1 design):
            the unified uploads + generated-artifact index with inline preview.
            R9-024 — plus the persona-scoped Calendar, coordinated so only one of
            the two right panels is open at a time. */}
        <ChatRightPanelGroup
          personaId={conv.persona_id}
          conversationId={conversationId}
          personaName={name}
        />
        {/* V7 D-V7-4 — one-click "Talk to {persona}" entry: drives the hoisted
            session (start / return-to-call / end-and-switch), not a bare route
            link (the V6 link relied on the surface auto-starting, removed in T3). */}
        <CallControl
          persona={{
            id: conv.persona_id,
            name,
            avatarUrl: personaRes.data?.avatar_url ?? undefined,
            role,
          }}
          conversationId={conversationId}
        />
      </div>
      {/* V7 D-V7-7: a web-derived "call ended · N min · view transcript" trace. */}
      <div className="px-4 pt-2">
        <CallRecap conversationId={conversationId} />
      </div>
      <ChatWindow
        conversationId={conversationId}
        persona={personaForDisplay}
        initialMessages={initialMessages}
        // F3 (T19) — D-F3-X-capabilities-prop-drill-shape: prop drill from
        // PersonaDetail → ChatWindow → ComposerAttachControl. NOT context,
        // NOT a global store. `capabilities` may be undefined when the
        // runtime isn't wired (test paths / pre-T02 deployments); the
        // composer treats that as "vision unknown" and falls open by
        // default (server stays authoritative — Spec 13 fail-loud refuses
        // image turns on text-only deployments per T15's (c) safety net).
        capabilities={personaRes.data?.capabilities ?? null}
        // R9-025 reopen — context-pinned dictation: `persona` above is
        // already the FULL parsed YAML (parsePersonaYaml), which already
        // extracts `identity.language_default` as `languageDefault`
        // (default "en") — reuse it rather than a second persona lookup.
        // `null` when there's no persona data at all (mic falls back to
        // auto-detect, same as today).
        personaLanguage={persona?.languageDefault ?? null}
      />
    </div>
  );
}
