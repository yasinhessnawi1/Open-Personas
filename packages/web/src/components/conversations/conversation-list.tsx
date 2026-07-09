"use client";

import { ChevronRight, MoreVertical, Trash2 } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo, useState } from "react";
import { PersonaAvatar } from "@/components/persona/persona-avatar";
import { useConfirm } from "@/components/providers/confirm-provider";
import { useNotify } from "@/components/providers/notification-provider";
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
export function ConversationList({
  conversations,
  personaById,
}: ConversationListProps) {
  const t = useTranslations("conversations");
  const tc = useTranslations("confirm");
  const tn = useTranslations("notifications");
  const confirm = useConfirm();
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

  const filtered = useMemo(() => {
    return conversations.filter((c) => {
      if (personaFilter && c.persona_id !== personaFilter) return false;
      if (qFilter && !(c.title ?? "").toLowerCase().includes(qFilter)) {
        return false;
      }
      return true;
    });
  }, [conversations, personaFilter, qFilter]);

  async function handleDelete(id: string, title: string) {
    if (deletingId) return;
    const label = title || t("untitled");
    const ok = await confirm({
      title: tc("deleteTitle", { name: label }),
      description: t("deleteConfirm", { title: label }),
      confirmLabel: tc("delete"),
      tone: "danger",
    });
    if (!ok) return;
    setDeletingId(id);
    try {
      await api.DELETE("/v1/conversations/{conversation_id}", {
        params: { path: { conversation_id: id } },
      });
      notify({ level: "success", title: tn("deleted", { name: label }) });
      // R9-012: the shared sidebar-refresh seam — the page list AND the
      // sidebar MESSAGES/badges re-resolve in one soft refresh.
      refreshSidebar();
    } finally {
      setDeletingId(null);
    }
  }

  if (filtered.length === 0) {
    return (
      <p className="type-body py-12 text-center text-muted-foreground">
        {t("noMatches")}
      </p>
    );
  }

  return (
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
                  onClick={() => handleDelete(c.id, c.title)}
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
  );
}
