"use client";

import { Search, X } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { CallHistoryPersona } from "./call-history-list";

export interface CallFilterStripProps {
  personas: readonly CallHistoryPersona[];
}

/**
 * R9-028 (c) — the calls-page persona-filter chip strip + title search input,
 * built with the R9-014 fix ALREADY applied (never regresses to the bugs that
 * fix closed): the persona chips live in a bounded horizontal scroll rail
 * (`.chip-rail`) that never grows/shrinks the search field beside it, and the
 * search icon sits INSIDE the input via `<Input startIcon>`. Byte-identical
 * structure to `<ConversationFilterStrip>` — this page just didn't have a
 * filter/search surface before R9-028, so it launches with the fix baked in
 * from day one instead of needing a follow-up.
 *
 * URL search params are the single source of truth (mirrors the conversations
 * page): `?persona_id=<id>` — persona-filter chip; `?q=<substr>` — title
 * substring search. `<CallHistoryList>` reads the same params and filters the
 * already-fetched calls client-side (no new API surface).
 */
export function CallFilterStrip({ personas }: CallFilterStripProps) {
  const t = useTranslations("calls");
  const router = useRouter();
  const search = useSearchParams();
  const activePersona = search.get("persona_id");

  const initialQ = search.get("q") ?? "";
  const [q, setQ] = useState(initialQ);

  useEffect(() => {
    setQ(initialQ);
  }, [initialQ]);

  useEffect(() => {
    // Guard against no-op navigation (the conversations-page fix: an
    // unconditional replace() loops forever via the fresh `search` ref every
    // navigation produces). Only navigate when q actually differs from the URL.
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
      data-slot="call-filter-strip"
    >
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
      <div className="flex w-40 shrink-0 items-center gap-1 sm:w-56">
        <Input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t("searchPlaceholder")}
          aria-label={t("searchPlaceholder")}
          data-slot="call-search"
          startIcon={<Search aria-hidden="true" />}
        />
        {q ? (
          <button
            type="button"
            onClick={() => setQ("")}
            aria-label={t("clearSearch")}
            className="shrink-0 rounded p-1 text-muted-foreground hover:text-foreground"
          >
            <X className="size-4" />
          </button>
        ) : null}
      </div>
    </div>
  );
}
