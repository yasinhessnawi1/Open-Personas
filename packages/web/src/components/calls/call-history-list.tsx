"use client";

/**
 * Spec V9 / R9-028 — the call-history list (the `/calls` page body).
 *
 * A flat reverse-chronological list of the caller's voice calls (the API
 * already returns `started_at DESC`). Each row: `<PersonaAvatar size="sm">` +
 * the call's (title_refresh-derived, R9-028) title as the PRIMARY line +
 * persona name · call time · duration as the secondary meta line — the SAME
 * title-leads-persona-trails row shape `<ConversationList>` uses, now that a
 * call carries a title too. The whole row links to the saved transcript at
 * `/chat/{conversation_id}` — the spoken turns persist as conversation
 * messages (V9-D-1/D-2), so the existing chat page renders them (no separate
 * transcript renderer). Calls are not deleted independently — a call-record
 * cascades with its conversation — so there is no per-row delete.
 *
 * R9-028 (c): client-side `?persona_id=` / `?q=` filtering — the SAME URL-param
 * convention + in-memory filter `<ConversationList>` uses (no new API surface;
 * the page already fetches the full page of calls server-side).
 */

import { ChevronRight, Phone } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";
import { PersonaAvatar } from "@/components/persona/persona-avatar";
import { formatCallDuration } from "@/lib/calls";

export interface CallHistoryPersona {
  id: string;
  name: string;
  avatar_url: string | null;
}

export interface CallHistoryItem {
  call_id: string;
  conversation_id: string;
  persona_id: string;
  title: string;
  started_at: string;
  duration_s: number | null;
}

export interface CallHistoryListProps {
  calls: readonly CallHistoryItem[];
  personaById: Record<string, CallHistoryPersona>;
}

export function CallHistoryList({ calls, personaById }: CallHistoryListProps) {
  const t = useTranslations("calls");
  const format = useFormatter();
  const search = useSearchParams();
  const personaFilter = search.get("persona_id");
  const qFilter = (search.get("q") ?? "").trim().toLowerCase();

  const filtered = useMemo(() => {
    return calls.filter((c) => {
      if (personaFilter && c.persona_id !== personaFilter) return false;
      if (qFilter && !(c.title ?? "").toLowerCase().includes(qFilter)) {
        return false;
      }
      return true;
    });
  }, [calls, personaFilter, qFilter]);

  if (filtered.length === 0) {
    return (
      <p className="type-body py-12 text-center text-muted-foreground">
        {t("noMatches")}
      </p>
    );
  }

  return (
    <ul className="flex flex-col" data-slot="call-history-list">
      {filtered.map((c) => {
        const persona = personaById[c.persona_id];
        const duration = formatCallDuration(c.duration_s);
        return (
          <li
            key={c.call_id}
            className="group/call flex items-center gap-3 border-b py-3"
            data-slot="call-row"
          >
            <Link
              href={`/chat/${c.conversation_id}`}
              className="flex min-w-0 flex-1 items-center gap-3"
            >
              {persona ? (
                <PersonaAvatar persona={persona} size="sm" />
              ) : (
                <span className="size-6 rounded-full bg-muted" aria-hidden />
              )}
              <span className="min-w-0 flex-1 flex-col">
                <span className="type-body block truncate font-medium">
                  {c.title || t("untitled")}
                </span>
                <span className="type-caption flex items-center gap-1 text-muted-foreground">
                  <Phone className="size-3 shrink-0" aria-hidden />
                  {persona ? persona.name : t("unknownPersona")}
                  {" · "}
                  {format.dateTime(new Date(c.started_at), {
                    dateStyle: "medium",
                    timeStyle: "short",
                  })}
                  {duration ? <span>{` · ${duration}`}</span> : null}
                </span>
              </span>
              <ChevronRight
                className="size-4 shrink-0 text-muted-foreground transition-transform group-hover/call:translate-x-0.5"
                aria-hidden="true"
              />
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
