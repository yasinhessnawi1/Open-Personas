"use client";

import { Search } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { type MemorySearchResult, unwrap } from "@/lib/api";
import { useApi } from "@/lib/api/use-api";

/**
 * Spec K5 — search-to-navigate (criterion 5). A debounced query over K1 hybrid
 * retrieval (`GET /v1/memory/search`, exact + paraphrase); a match selects its
 * node, which the canvas flies to. Results render as a small list; Enter jumps to
 * the top match. The retrieval (dense + sparse fusion) stays in K1 — this is a
 * thin projection (the user sees their own K4-flagged nodes here too, criterion 8).
 */
const DEBOUNCE_MS = 220;

export function MemorySearch({ onSelect }: { onSelect: (id: string) => void }) {
  const t = useTranslations("memory");
  const api = useApi();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<MemorySearchResult[]>([]);
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);
  // `useApi()` is not a stable identity; keep it in a ref so the debounced effect
  // depends only on `query` (api in the deps would re-fire the effect every render
  // → an infinite setState loop).
  const apiRef = useRef(api);
  apiRef.current = api;

  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setResults((prev) => (prev.length ? [] : prev));
      return;
    }
    let cancelled = false;
    const handle = setTimeout(async () => {
      try {
        const data = await unwrap(
          await apiRef.current.GET("/v1/memory/search", {
            params: { query: { q } },
          }),
        );
        if (!cancelled) {
          setResults(data.results);
          setOpen(true);
        }
      } catch {
        if (!cancelled) setResults([]);
      }
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [query]);

  // dismiss the dropdown on outside click
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node))
        setOpen(false);
    };
    document.addEventListener("pointerdown", onClick);
    return () => document.removeEventListener("pointerdown", onClick);
  }, []);

  const pick = (id: string) => {
    onSelect(id);
    setOpen(false);
  };

  return (
    <div ref={boxRef} className="relative w-full max-w-md">
      <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onFocus={() => results.length > 0 && setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && results[0]) pick(results[0].node_id);
          if (e.key === "Escape") {
            setQuery("");
            setOpen(false);
          }
        }}
        placeholder={t("searchPlaceholder")}
        aria-label={t("searchLabel")}
        className="h-9 w-full rounded-lg border border-input bg-background pl-9 pr-20 text-sm focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/40"
      />
      {results.length > 0 ? (
        <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 rounded border bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
          {t("flyToHint")}
        </span>
      ) : null}
      {open && results.length > 0 ? (
        <ul className="absolute z-30 mt-1.5 max-h-80 w-full overflow-y-auto rounded-lg border bg-popover p-1 shadow-[var(--elevation-2)]">
          {results.map((r) => (
            <li key={r.node_id}>
              <button
                type="button"
                onClick={() => pick(r.node_id)}
                className="flex w-full items-center justify-between gap-3 rounded-md px-2.5 py-2 text-left hover:bg-muted"
              >
                <span className="truncate text-sm">{r.label}</span>
                <span className="type-caption shrink-0 normal-case text-muted-foreground">
                  {t(`kind.${r.kind}`)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
