"use client";

import { Search, X } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { ConversationListPersona } from "./conversation-list";

export interface ConversationFilterStripProps {
  personas: readonly ConversationListPersona[];
}

/**
 * Spec F5 T13 — persona-filter chip strip + title search input.
 *
 * URL search params are the single source of truth for filter state per
 * D-F5-X-conversation-history-pagination + frontend-patterns audit:
 *   ?persona_id=<id> — persona-filter chip
 *   ?q=<substr>      — title substring search
 *
 * Glass-chip aesthetic per aesthetic-direction.md §3.2; identity-coloured
 * border-bottom on active state per D-F1-5.
 */
export function ConversationFilterStrip({
  personas,
}: ConversationFilterStripProps) {
  const t = useTranslations("conversations");
  const router = useRouter();
  const search = useSearchParams();
  const activePersona = search.get("persona_id");

  // Debounced search input — keep typing snappy, push URL on settle.
  const initialQ = search.get("q") ?? "";
  const [q, setQ] = useState(initialQ);

  useEffect(() => {
    setQ(initialQ);
  }, [initialQ]);

  useEffect(() => {
    // Guard against no-op navigation. `search` (useSearchParams) is in the deps
    // and gets a fresh reference after every navigation, so an unconditional
    // router.replace() would loop forever: replace → RSC refetch → new `search`
    // ref → effect re-fires → replace … (GET /conversations indefinitely, even
    // with empty input). Only navigate when q actually differs from the URL's q.
    const current = (search.get("q") ?? "").trim();
    if (q.trim() === current) return;
    const handle = setTimeout(() => {
      const next = new URLSearchParams(search.toString());
      if (q.trim()) next.set("q", q.trim());
      else next.delete("q");
      const href = next.toString();
      router.replace(href ? `?${href}` : "?", { scroll: false });
    }, 300);
    return () => clearTimeout(handle);
  }, [q, search, router]);

  function setPersona(id: string | null) {
    const next = new URLSearchParams(search.toString());
    if (id) next.set("persona_id", id);
    else next.delete("persona_id");
    const href = next.toString();
    router.replace(href ? `?${href}` : "?", { scroll: false });
  }

  const chips = useMemo(
    () => [
      { id: null, label: t("allPersonas") } as const,
      ...personas.map((p) => ({ id: p.id, label: p.name })),
    ],
    [personas, t],
  );

  return (
    <div
      className="flex min-w-0 flex-1 items-center gap-3"
      data-slot="conversation-filter-strip"
    >
      {/*
        R9-014 (a): the persona chips live in a BOUNDED horizontal scroll rail
        (`.chip-rail` — overflow-x-auto, scrollbar hidden, subtle edge fade). It
        takes the remaining row width (`min-w-0 flex-1`) and scrolls internally,
        so adding personas can never shrink the search field beside it. A
        scroll-x rail (over a "+N more" overflow menu) matches the sidebar's
        existing `.v-rail__scroll` language and keeps every persona one swipe
        away. URL-param filter behaviour is unchanged: a chip click still sets
        ?persona_id=.
      */}
      <div className="chip-rail min-w-0 flex-1" data-slot="filter-chip-rail">
        <div className="flex w-max items-center gap-2">
          {chips.map((c) => {
            const active = (c.id ?? null) === activePersona;
            return (
              <button
                key={c.id ?? "__all__"}
                type="button"
                onClick={() => setPersona(c.id)}
                data-state={active ? "active" : "inactive"}
                className={cn("glass-chip shrink-0", active && "type-ui")}
              >
                {c.label}
              </button>
            );
          })}
        </div>
      </div>
      {/* Stable-width search — `shrink-0` so the chip count never resizes it. */}
      <div className="flex w-40 shrink-0 items-center gap-1 sm:w-56">
        <Input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t("searchPlaceholder")}
          aria-label={t("searchPlaceholder")}
          data-slot="conversation-search"
          startIcon={<Search aria-hidden="true" />}
        />
        {q ? (
          <button
            type="button"
            onClick={() => setQ("")}
            aria-label={t("delete")}
            className="shrink-0 rounded p-1 text-muted-foreground hover:text-foreground"
          >
            <X className="size-4" />
          </button>
        ) : null}
      </div>
    </div>
  );
}
