"use client";

import {
  FileText,
  MessageSquare,
  MoreVertical,
  Phone,
  Shield,
  Wrench,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { startChat } from "@/app/actions";
import { PersonaAvatar } from "@/components/persona/persona-avatar";
import type { PersonaCardPersona } from "@/components/persona/persona-card";
import { useConfirm } from "@/components/providers/confirm-provider";
import { useNotify } from "@/components/providers/notification-provider";
import { buttonVariants } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ActiveCallIndicator } from "@/components/voice/active-call-indicator";
import { useApi } from "@/lib/api/use-api";
import { useSidebarRefresh } from "@/lib/hooks/use-sidebar-refresh";
import { renameInIdentity } from "@/lib/persona";
import { personaIdentityStyle } from "@/lib/persona-identity";
import { cn } from "@/lib/utils";
import {
  type CallTarget,
  useCallSession,
} from "@/lib/voice/call-session-context";

/**
 * Spec F5 T09 — Persona library card wrapper.
 *
 * Composes the F2 `<PersonaCard>` with:
 *   1. The Phase 4-locked glass aesthetic (via the F1-amended `.glass-card`
 *      utility added in T08; D-F5-X-glass-token-f1-amendment).
 *   2. An action `<DropdownMenu>` (kebab top-right) per D-F5-X-persona-library-
 *      design-language: View / Edit / Duplicate / Delete. `<Sheet>`-based
 *      confirmations for Duplicate + Delete are wired here as a v0.1
 *      simple-confirm pattern (the richer F2 `<Sheet>` confirmation flow
 *      per D-F5-X-persona-duplicate-flow lands at T11 — this T09 ships the
 *      structural surface so the page extension is observable end-to-end).
 *
 * F5-local at v0.1; promotes to F2 on second-consumer per the strangler-fig
 * discipline (mirrors D-F3-X-chip-placement / D-F4-X-result-block-placement).
 */
export interface PersonaLibraryCardProps {
  /**
   * The list-view persona. Spec 35 surfaces the capability/identity glance from
   * the (free) `PersonaSummary` counts — `tools_count` already folds MCP servers
   * (a persona enables a server via `mcp:<name>` in its tools), so it reads as
   * one "apps & tools" count.
   */
  persona: PersonaCardPersona & {
    language?: string;
    tools_count?: number;
    skills_count?: number;
    constraints_count?: number;
    conversation_count?: number;
  };
}

export function PersonaLibraryCard({ persona }: PersonaLibraryCardProps) {
  const t = useTranslations("personas");
  const tc = useTranslations("confirm");
  const tn = useTranslations("notifications");
  const confirm = useConfirm();
  const { notify } = useNotify();
  const router = useRouter();
  const refreshSidebar = useSidebarRefresh();
  const api = useApi();
  const { requestCall } = useCallSession();
  const [busy, setBusy] = useState(false);

  // Spec V7 D-V7-4 / T4b — route the library "call" entry through the hoisted
  // session so it CAN'T bypass the one-call rule. Voice hangs off a conversation,
  // so we mint one first (as the old `startVoice` server action did), then ask
  // the session to place the call: if another call is live, `requestCall` opens
  // the end-and-switch confirm (which navigates on confirm) instead of redirecting
  // into a dead voice page behind the active call's Room.
  async function handleCall() {
    if (busy) return;
    setBusy(true);
    try {
      // V9 (V9-D-3 / V9-D-X-marker-writer-web): mark the conversation call-born
      // at creation. ``origin`` is the immutable birth-marker + the ONLY seam
      // between chat and voice — a call-born conversation is excluded from the
      // chat list and surfaces in Calls. Text-create paths omit it (server
      // default 'chat').
      const conv = await api.POST("/v1/personas/{persona_id}/conversations", {
        params: { path: { persona_id: persona.id } },
        body: { title: "", origin: "call" },
      });
      if (!conv.data) return;
      const target: CallTarget = {
        personaId: persona.id,
        conversationId: conv.data.id,
        personaName: persona.name,
        personaAvatarUrl: persona.avatar_url ?? undefined,
        personaRole: persona.role,
      };
      if (requestCall(target) !== "switch") {
        router.push(`/chat/${conv.data.id}/voice`);
      }
    } finally {
      setBusy(false);
    }
  }

  async function handleDuplicate() {
    if (busy) return;
    const ok = await confirm({
      title: tc("duplicateTitle", { name: persona.name }),
      description: t("library.duplicateConfirm", { name: persona.name }),
      confirmLabel: tc("duplicate"),
    });
    if (!ok) return;
    setBusy(true);
    try {
      // Fetch the original persona's full YAML, mutate identity.name to add
      // " (copy)", and POST as a new persona per D-F5-4 (definition-only:
      // identity / self_facts / worldview / constraints / tools / skills
      // carry; persona_id resets server-side; memory + conversations fresh).
      const original = await api.GET("/v1/personas/{persona_id}", {
        params: { path: { persona_id: persona.id } },
      });
      if (!original.data) return;
      const newYaml = renameInIdentity(
        original.data.yaml,
        `${persona.name} (copy)`,
      );
      await api.POST("/v1/personas", {
        body: { yaml: newYaml, avatar_url: null },
      });
      notify({
        level: "success",
        title: tn("duplicated", { name: persona.name }),
      });
      // R9-012: the shared sidebar-refresh seam — the PERSONAS rail + badges
      // re-resolve server-side (soft refresh; client state preserved).
      refreshSidebar();
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete() {
    if (busy) return;
    const ok = await confirm({
      title: tc("deleteTitle", { name: persona.name }),
      description: t("library.deleteConfirm", { name: persona.name }),
      confirmLabel: tc("delete"),
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await api.DELETE("/v1/personas/{persona_id}", {
        params: { path: { persona_id: persona.id } },
      });
      notify({
        level: "success",
        title: tn("deleted", { name: persona.name }),
      });
      refreshSidebar(); // R9-012: shared sidebar-refresh seam
    } finally {
      setBusy(false);
    }
  }

  return (
    <article
      className="v-card v-card--pad v-card--hover relative"
      style={personaIdentityStyle(persona)}
      data-slot="persona-library-card"
    >
      {/* Card body → the persona detail. Only the header is the navigating link
          so the footer actions + kebab stay independently clickable. */}
      <Link
        href={`/personas/${persona.id}`}
        className="block outline-none"
        data-slot="persona-library-card-link"
      >
        <div className="flex items-start gap-3.5">
          <PersonaAvatar persona={persona} size="lg" />
          <div className="min-w-0 flex-1 pr-8">
            <p className="type-heading leading-tight">
              <span className="v-id-underline">{persona.name}</span>
            </p>
            <p className="mt-1 truncate type-ui text-muted-foreground">
              {persona.role}
            </p>
          </div>
        </div>
      </Link>

      {/* Capability + identity glance — the free PersonaSummary counts. The
          "apps & tools" count already folds MCP servers (mcp:<name> in tools). */}
      <div className="mt-3.5 flex flex-wrap items-center gap-1.5">
        {persona.language ? (
          <span className="v-chip uppercase">{persona.language}</span>
        ) : null}
        {persona.tools_count ? (
          <span
            className="v-chip"
            title={t("library.appsTools", { count: persona.tools_count })}
          >
            <Wrench aria-hidden="true" />
            {persona.tools_count}
          </span>
        ) : null}
        {persona.skills_count ? (
          <span
            className="v-chip"
            title={t("library.skillsCount", { count: persona.skills_count })}
          >
            <FileText aria-hidden="true" />
            {persona.skills_count}
          </span>
        ) : null}
        {persona.constraints_count ? (
          <span
            className="v-chip"
            title={t("library.constraintsCount", {
              count: persona.constraints_count,
            })}
          >
            <Shield aria-hidden="true" />
            {persona.constraints_count}
          </span>
        ) : null}
      </div>

      {/* Footer: chat count + entry actions — voice routes through the call
          session (T4b, one-call-safe); text chat via the startChat server action. */}
      <footer className="mt-4 flex items-center gap-2 border-t pt-3.5">
        <span className="type-caption normal-case tracking-normal text-muted-foreground">
          {t("library.chats", { count: persona.conversation_count ?? 0 })}
        </span>
        {/* V7 D-V7-5: a live cue + one-tap return, only when THIS persona is on a call. */}
        <ActiveCallIndicator personaId={persona.id} />
        <button
          type="button"
          onClick={handleCall}
          disabled={busy}
          className="v-btn v-btn--ghost v-btn--sm ml-auto"
          aria-label={t("library.call")}
        >
          <Phone className="size-4" aria-hidden="true" />
        </button>
        <form action={startChat.bind(null, persona.id)}>
          <button type="submit" className="v-btn v-btn--outline v-btn--sm">
            <MessageSquare className="size-4" aria-hidden="true" />
            {t("library.chat")}
          </button>
        </form>
      </footer>

      <div className="absolute top-2 right-2">
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label={t("library.menuLabel", { name: persona.name })}
            className={cn(
              buttonVariants({ variant: "ghost", size: "icon" }),
              "size-8",
            )}
            data-slot="persona-library-card-menu"
            onClick={(e) => {
              // Stop propagation so the wrapping <Link> in PersonaCard
              // doesn't navigate when the menu trigger is clicked.
              e.stopPropagation();
              e.preventDefault();
            }}
          >
            <MoreVertical className="size-4" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem
              render={<Link href={`/personas/${persona.id}`} />}
            >
              {t("library.view")}
            </DropdownMenuItem>
            <DropdownMenuItem disabled={busy} onClick={handleDuplicate}>
              {t("library.duplicate")}
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={busy}
              variant="destructive"
              onClick={handleDelete}
            >
              {t("library.delete")}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </article>
  );
}
