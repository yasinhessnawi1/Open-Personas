"use client";

import { Dialog } from "@base-ui/react/dialog";
import { ChevronRight, MoreVertical, Trash2 } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo, useState } from "react";
import { PersonaAvatar } from "@/components/persona/persona-avatar";
import { useNotify } from "@/components/providers/notification-provider";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useApi } from "@/lib/api/use-api";
import { useSidebarRefresh } from "@/lib/hooks/use-sidebar-refresh";
import { cn } from "@/lib/utils";

export interface ConversationListPersona {
  id: string;
  name: string;
  avatar_url: string | null;
}

export interface ConversationListItem {
  id: string;
  persona_id: string;
  title: string;
  updated_at: string;
}

export interface ConversationListProps {
  conversations: readonly ConversationListItem[];
  personaById: Record<string, ConversationListPersona>;
}

/**
 * Spec F5 T12 — Conversation row list with row-hover delete trigger.
 *
 * Composes F2 `<PersonaAvatar size="sm">` + lucide chevron + per-row
 * `<DropdownMenu>` delete affordance. URL search-params filter shape
 * (T13: ?persona_id= + ?q=) reads from `useSearchParams` so the list is
 * shareable + back-button friendly.
 */
/** The row awaiting the delete confirm — enough to render the K11-T5 dialog. */
interface PendingDelete {
  id: string;
  title: string;
  personaName: string | null;
}

export function ConversationList({
  conversations,
  personaById,
}: ConversationListProps) {
  const t = useTranslations("conversations");
  const tc = useTranslations("confirm");
  const tn = useTranslations("notifications");
  const { notify } = useNotify();
  // next-intl's formatter is pinned to the active locale on both server + client,
  // so the rendered date matches (a bare `toLocaleDateString()` used the runtime
  // default locale, which differs SSR↔browser → hydration mismatch).
  const format = useFormatter();
  const api = useApi();
  const refreshSidebar = useSidebarRefresh();
  const search = useSearchParams();
  const personaFilter = search.get("persona_id");
  const qFilter = (search.get("q") ?? "").trim().toLowerCase();
  const [deletingId, setDeletingId] = useState<string | null>(null);
  // K11-T5 (D-K11-9): the delete confirm gains an opt-in "also forget"
  // checkbox — `useConfirm()`'s plain title/description shape can't host it,
  // so this is its own small dialog (mirrors the other bespoke dialogs in
  // this codebase, e.g. `persona-memories-modal.tsx`).
  const [pending, setPending] = useState<PendingDelete | null>(null);
  const [forgetMemory, setForgetMemory] = useState(false);

  const filtered = useMemo(() => {
    return conversations.filter((c) => {
      if (personaFilter && c.persona_id !== personaFilter) return false;
      if (qFilter && !(c.title ?? "").toLowerCase().includes(qFilter)) {
        return false;
      }
      return true;
    });
  }, [conversations, personaFilter, qFilter]);

  function requestDelete(
    id: string,
    title: string,
    personaName: string | null,
  ) {
    if (deletingId) return;
    setForgetMemory(false);
    setPending({ id, title, personaName });
  }

  async function confirmDelete() {
    if (!pending) return;
    const { id, title } = pending;
    const label = title || t("untitled");
    setPending(null);
    setDeletingId(id);
    try {
      await api.DELETE("/v1/conversations/{conversation_id}", {
        params: {
          path: { conversation_id: id },
          query: { forget_memory: forgetMemory },
        },
      });
      notify({ level: "success", title: tn("deleted", { name: label }) });
      // R9-012: the shared sidebar-refresh seam — the page list AND the
      // sidebar MESSAGES/badges re-resolve in one soft refresh.
      refreshSidebar();
    } finally {
      setDeletingId(null);
    }
  }

  const deleteDialog = (
    <Dialog.Root
      open={pending !== null}
      onOpenChange={(open) => {
        if (!open) setPending(null);
      }}
    >
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-[80] bg-black/40 transition-opacity duration-[var(--motion-duration-fast)] data-ending-style:opacity-0 data-starting-style:opacity-0 supports-backdrop-filter:backdrop-blur-xs" />
        <Dialog.Popup
          data-slot="delete-conversation-dialog"
          className="-translate-x-1/2 -translate-y-1/2 fixed top-1/2 left-1/2 z-[80] flex w-[min(28rem,calc(100vw-2rem))] flex-col gap-3 rounded-xl border bg-popover bg-clip-padding p-5 text-popover-foreground shadow-[var(--elevation-3)]"
        >
          <Dialog.Title className="font-heading font-medium text-base text-foreground">
            {tc("deleteTitle", { name: pending?.title || t("untitled") })}
          </Dialog.Title>
          <Dialog.Description className="text-muted-foreground text-sm">
            {t("deleteConfirm", { title: pending?.title || t("untitled") })}
          </Dialog.Description>
          {pending?.personaName ? (
            <label className="flex items-start gap-2.5 rounded-lg border border-border bg-background p-2.5 text-sm">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={forgetMemory}
                onChange={(e) => setForgetMemory(e.target.checked)}
              />
              <span>
                {t("forgetMemoryCheckbox", { name: pending.personaName })}
              </span>
            </label>
          ) : null}
          <div className="mt-2 flex justify-end gap-2">
            <Button variant="outline" onClick={() => setPending(null)}>
              {tc("cancel")}
            </Button>
            <Button variant="destructive" onClick={confirmDelete}>
              {tc("delete")}
            </Button>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );

  if (filtered.length === 0) {
    return (
      <>
        <p className="type-body py-12 text-center text-muted-foreground">
          {t("noMatches")}
        </p>
        {deleteDialog}
      </>
    );
  }

  return (
    <>
      <ul className="flex flex-col" data-slot="conversation-list">
        {filtered.map((c) => {
          const persona = personaById[c.persona_id];
          return (
            <li
              key={c.id}
              className={cn(
                "group/conv flex items-center gap-3 border-b py-3",
                deletingId === c.id && "opacity-50",
              )}
              data-slot="conversation-row"
            >
              <Link
                href={`/chat/${c.id}`}
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
                  <span className="type-caption text-muted-foreground">
                    {persona ? persona.name : t("unknownPersona")}
                    {" · "}
                    {format.dateTime(new Date(c.updated_at), {
                      dateStyle: "medium",
                    })}
                  </span>
                </span>
                <ChevronRight
                  className="size-4 shrink-0 text-muted-foreground transition-transform group-hover/conv:translate-x-0.5"
                  aria-hidden="true"
                />
              </Link>
              <DropdownMenu>
                <DropdownMenuTrigger
                  aria-label={t("rowMenuLabel", {
                    title: c.title || t("untitled"),
                  })}
                  className="rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:bg-muted hover:text-foreground focus:opacity-100 group-hover/conv:opacity-100"
                  data-slot="conversation-row-menu"
                >
                  <MoreVertical className="size-4" />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem
                    variant="destructive"
                    disabled={deletingId === c.id}
                    onClick={() =>
                      requestDelete(c.id, c.title, persona?.name ?? null)
                    }
                  >
                    <Trash2 className="mr-2 size-4" />
                    {t("delete")}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </li>
          );
        })}
      </ul>
      {deleteDialog}
    </>
  );
}
