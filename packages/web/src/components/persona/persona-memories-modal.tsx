"use client";

import { Dialog } from "@base-ui/react/dialog";
import { ArrowUpRight } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { SkeletonBlock } from "@/components/patterns/loading";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api/use-api";

interface MemoryItem {
  id: string;
  name: string;
  content: string;
  created_at: string;
  conversation_id?: string | null;
}

/**
 * R11-B6 rider (owner-ruled) — the glance's Memories row opens THIS modal:
 * the persona's graph memories, newest first, each deep-linking to its
 * originating conversation (when the provenance carries one) and to the
 * Memory graph. Wired end-to-end on `GET /v1/personas/{id}/memories`;
 * `available=false` reads as the memory area's no-graph state, never as
 * "no memories yet".
 */
export function PersonaMemoriesModal({
  personaId,
  count,
  trigger,
}: {
  personaId: string;
  count: number;
  trigger: React.ReactElement;
}) {
  const t = useTranslations("personaPage.memoriesModal");
  // Locale-pinned both sides — see conversation-list.tsx / R9-044.
  const format = useFormatter();
  const api = useApi();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<{
    available: boolean;
    total: number;
    items: MemoryItem[];
  } | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!open || data !== null) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await api.GET("/v1/personas/{persona_id}/memories", {
          params: { path: { persona_id: personaId } },
        });
        if (!cancelled) {
          if (res.data) setData(res.data);
          else setError(true);
        }
      } catch {
        if (!cancelled) setError(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, data, api, personaId]);

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger render={trigger} />
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-50 bg-black/30 transition-opacity duration-[var(--motion-duration-fast)] data-ending-style:opacity-0 data-starting-style:opacity-0 supports-backdrop-filter:backdrop-blur-xs" />
        <Dialog.Popup className="-translate-x-1/2 fixed top-[10vh] left-1/2 z-50 flex max-h-[75vh] w-[min(36rem,calc(100vw-2rem))] flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-xl outline-none">
          <div className="flex items-baseline gap-2 border-border border-b px-5 py-4">
            <Dialog.Title className="font-heading text-lg font-semibold">
              {t("title")}
            </Dialog.Title>
            <span className="rounded-full border border-border/60 bg-muted px-1.5 py-px text-[11px] tabular-nums text-muted-foreground">
              {count}
            </span>
            <Link
              href="/memory"
              className="ml-auto flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
            >
              {t("openGraph")}
              <ArrowUpRight className="size-3.5" aria-hidden="true" />
            </Link>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            {error ? (
              <p className="py-6 text-center text-sm text-destructive">
                {t("loadFailed")}
              </p>
            ) : data === null ? (
              <div className="flex flex-col gap-2">
                <SkeletonBlock className="h-14" />
                <SkeletonBlock className="h-14" />
              </div>
            ) : !data.available ? (
              <p className="py-6 text-center text-sm text-muted-foreground">
                {t("unavailable")}
              </p>
            ) : data.items.length === 0 ? (
              <p className="py-6 text-center text-sm text-muted-foreground">
                {t("empty")}
              </p>
            ) : (
              <ul className="flex flex-col gap-2">
                {data.items.map((m) => (
                  <li
                    key={m.id}
                    className="rounded-lg border border-border bg-background p-3"
                  >
                    <div className="flex items-baseline gap-2">
                      <p className="min-w-0 flex-1 truncate text-sm font-medium">
                        {m.name}
                      </p>
                      <span className="type-caption normal-case tracking-normal shrink-0 text-muted-foreground">
                        {format.dateTime(new Date(m.created_at), {
                          dateStyle: "medium",
                        })}
                      </span>
                    </div>
                    <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">
                      {m.content}
                    </p>
                    {m.conversation_id ? (
                      <Link
                        href={`/chat/${m.conversation_id}`}
                        className="mt-1.5 inline-flex items-center gap-1 text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                      >
                        {t("openConversation")}
                        <ArrowUpRight className="size-3" aria-hidden="true" />
                      </Link>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
            {data && data.total > data.items.length ? (
              <p className="type-caption normal-case tracking-normal pt-3 text-center text-muted-foreground">
                {t("more", { count: data.total - data.items.length })}
              </p>
            ) : null}
          </div>
          <div className="border-border border-t px-5 py-3">
            <Dialog.Close
              render={
                <Button type="button" variant="ghost" className="w-full">
                  {t("close")}
                </Button>
              }
            />
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
