import { MessageSquare, Mic } from "lucide-react";
import Link from "next/link";
import { getTranslations } from "next-intl/server";

import { startChat, startVoice } from "@/app/actions";
import {
  type AvatarPersona,
  PersonaAvatar,
} from "@/components/persona/persona-avatar";
import { personaIdentityStyle } from "@/lib/persona-identity";

/**
 * Spec R11 (B2, D-R11-5) — the Activity quick-access strip: the useful part of
 * the retired Home dashboard (persona quick-launch + resume-a-conversation),
 * folded into the review in the SAME quiet register as the kit's "Upcoming"
 * strip — a muted panel, small type, horizontal track. It sits BELOW the
 * triage sections so it never competes with waiting-first ordering; the strip
 * is a convenience, not a summons.
 *
 * Server component: Chat/Call reuse the root server actions (mint a fresh
 * conversation), resume rows deep-link into the existing chat.
 */

export interface QuickAccessPersona extends AvatarPersona {
  readonly name: string;
}

export interface QuickAccessConversation {
  readonly id: string;
  readonly title: string | null;
  readonly persona: (AvatarPersona & { readonly name: string }) | null;
}

export async function QuickAccess({
  personas,
  conversations,
}: {
  personas: readonly QuickAccessPersona[];
  conversations: readonly QuickAccessConversation[];
}) {
  const t = await getTranslations("review.quickAccess");
  if (personas.length === 0) return null;

  return (
    <section
      className="mt-8 flex flex-col gap-3 rounded-lg border border-border/70 bg-muted/40 p-4"
      data-slot="quick-access"
      aria-label={t("heading")}
    >
      <h3 className="type-caption font-medium uppercase tracking-wide text-muted-foreground">
        {t("heading")}
      </h3>

      <div className="flex gap-2 overflow-x-auto pb-1">
        {personas.map((p) => (
          <div
            key={p.id}
            className="flex shrink-0 items-center gap-2 rounded-md border border-border bg-card py-1.5 pl-2 pr-1.5"
            style={personaIdentityStyle(p)}
          >
            <PersonaAvatar persona={p} size="sm" />
            <span className="max-w-32 truncate text-sm font-medium">
              {p.name}
            </span>
            <form action={startChat.bind(null, p.id)} className="flex">
              <button
                type="submit"
                aria-label={t("chatWith", { name: p.name })}
                className="grid size-7 place-items-center rounded text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
              >
                <MessageSquare className="size-3.5" aria-hidden="true" />
              </button>
            </form>
            <form action={startVoice.bind(null, p.id)} className="flex">
              <button
                type="submit"
                aria-label={t("callWith", { name: p.name })}
                className="grid size-7 place-items-center rounded text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Mic className="size-3.5" aria-hidden="true" />
              </button>
            </form>
          </div>
        ))}
      </div>

      {conversations.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {conversations.map((c) => (
            <li key={c.id}>
              <Link
                href={`/chat/${c.id}`}
                className="group flex items-center gap-2 rounded px-1 py-0.5 text-sm text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
              >
                {c.persona ? (
                  <span style={personaIdentityStyle(c.persona)}>
                    <PersonaAvatar persona={c.persona} size="sm" />
                  </span>
                ) : null}
                <span className="truncate group-hover:underline">
                  {c.title?.trim() || t("untitled")}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
