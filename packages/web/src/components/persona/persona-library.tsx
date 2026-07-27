"use client";

import { Plus, Search } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";
import { Grid } from "@/components/layout";
import type { PersonaSummary } from "@/lib/api";
import { PersonaLibraryCard } from "./persona-library-card";

/**
 * R11-B6 — the library body in the kit register (`personas.html`): a live
 * search over the grid, an honest count line, and the dashed "New persona"
 * tile closing the grid ("Describe one in a sentence — we'll draft the rest.").
 * The cards themselves stay `PersonaLibraryCard` (kebab minus Edit — the
 * persona page IS the editor now).
 */
export function PersonaLibrary({ personas }: { personas: PersonaSummary[] }) {
  const t = useTranslations("personas");
  const [query, setQuery] = useState("");

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return personas;
    return personas.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        (p.role ?? "").toLowerCase().includes(q),
    );
  }, [personas, query]);

  return (
    <div className="flex flex-col gap-4" data-slot="persona-library">
      <div className="relative">
        <Search
          className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden="true"
        />
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("searchPlaceholder")}
          aria-label={t("searchPlaceholder")}
          className="h-10 w-full rounded-full border border-border bg-background pl-9 pr-4 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>
      <p className="type-caption text-muted-foreground">
        {t("count", { count: visible.length })}
      </p>
      <Grid cols={{ base: 1, sm: 2, lg: 3 }} gap={4}>
        {visible.map((p) => (
          <PersonaLibraryCard key={p.id} persona={p} />
        ))}
        {!query ? (
          <Link
            href="/personas/new"
            className="flex min-h-44 flex-col items-center justify-center gap-2 rounded-xl border-[1.5px] border-dashed border-border p-6 text-center outline-none transition-colors hover:border-primary/50 focus-visible:ring-2 focus-visible:ring-ring"
            data-slot="new-persona-tile"
          >
            <span className="grid size-10 place-items-center rounded-full bg-muted">
              <Plus className="size-5" aria-hidden="true" />
            </span>
            <span className="font-heading font-semibold">{t("create")}</span>
            <span className="text-sm text-muted-foreground">
              {t("createTileHint")}
            </span>
          </Link>
        ) : null}
      </Grid>
      {query && visible.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          {t("noMatches")}
        </p>
      ) : null}
    </div>
  );
}
