"use client";

/**
 * The app-sidebar section bodies: the PERSONAS rail and the MESSAGES list.
 *
 * Both are pure presentational client components fed server-resolved data
 * (`SidebarData`). They are collapse-aware: when the sidebar is an icon rail
 * (`collapsed`), each renders an icon-only, tooltip-labelled treatment; when
 * expanded they render the full label + brief. Keeping the two modes in one
 * component keeps the avatar identity (colour + initials) continuous across the
 * collapse animation.
 */

import { MessageSquare, MessagesSquare, Phone } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";
import { startChat } from "@/app/actions";
import { PressSwipePreview } from "@/components/patterns/press-swipe-preview";
import { PersonaAvatar } from "@/components/persona/persona-avatar";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ActiveChatIndicator } from "@/components/work/active-chat-indicator";
import { useApi } from "@/lib/api/use-api";
import { formatCallDuration } from "@/lib/calls";
import { usePressSwipeGesture } from "@/lib/hooks/use-press-swipe-gesture";
import { personaIdentityStyle } from "@/lib/persona-identity";
import { cn } from "@/lib/utils";
import {
  type CallTarget,
  useCallSession,
} from "@/lib/voice/call-session-context";
import type {
  SidebarCall,
  SidebarConversation,
  SidebarPersona,
} from "./sidebar-data";

/**
 * PERSONAS — a compact, fixed-height rail of the most-recent personas for fast
 * access. Expanded: a wrapping row of avatar chips. Collapsed: a vertical
 * stack of avatars. Each links to the persona's page (`/personas/:id`), the
 * same target the rest of the app uses.
 *
 * R9-036 REOPEN: each avatar is now ALSO gesture-capable (a real swipe —
 * vertical drag past a small tap-slop shows a call/chat live preview) — see
 * `PersonaRailItem` below. Both collapsed and expanded modes share this ONE
 * component/hook pairing (no forked logic).
 */
export function PersonasRail({
  personas,
  collapsed,
  onNavigate,
}: {
  personas: readonly SidebarPersona[];
  collapsed: boolean;
  onNavigate?: () => void;
}) {
  if (personas.length === 0) return null;
  return (
    <ul
      className={cn(
        "flex gap-1.5",
        collapsed ? "flex-col items-center" : "flex-row flex-wrap",
      )}
      data-slot="sidebar-personas-rail"
    >
      {personas.map((p) => (
        <PersonaRailItem
          key={p.id}
          persona={p}
          collapsed={collapsed}
          onNavigate={onNavigate}
        />
      ))}
    </ul>
  );
}

/**
 * One PERSONAS-rail avatar (R9-036 REOPEN): the pre-existing click-through
 * Link, now ALSO driving `usePressSwipeGesture` + `<PressSwipePreview>` —
 * the reusable swipe mechanic (lib/hooks/use-press-swipe-gesture.ts,
 * components/patterns/press-swipe-preview.tsx). NO hold, no timer: vertical
 * movement past a small tap-slop immediately shows the live call/chat
 * preview. The commit geometry is the avatar's OWN circle — dragging past
 * its rendered radius (measured from the element's own rect) locks the
 * direction; releasing while locked commits, dragging back inside or
 * releasing unlocked cancels. A plain tap (release under slop) is
 * BYTE-IDENTICAL to the prior click-through — the gesture hook never
 * touches that path.
 *
 * Scroll trade-off (owner-ruled): the avatar Link carries `touch-none`
 * UNCONDITIONALLY — a vertical drag starting on an avatar always belongs to
 * this gesture, never to the sidebar's own scroll. The rest of the rail
 * (non-avatar space) and wheel scrolling are unaffected.
 *
 * Both actions go through the app's EXISTING origination seams (no new
 * flow):
 *   - call: mint an `origin: 'call'` conversation, then hand it to the
 *     hoisted call session via `requestCall` — the identical sequence
 *     `persona-library-card.tsx`'s `handleCall` and R9-028's
 *     `new-call-button.tsx` already use (V7 D-V7-4: the one-call rule lives
 *     in `requestCall` itself, so this can't bypass it).
 *   - chat: the `startChat` server action (app/actions.ts) — the identical
 *     call R9-014's `new-conversation-button.tsx` picker flow already makes.
 */
function PersonaRailItem({
  persona,
  collapsed,
  onNavigate,
}: {
  persona: SidebarPersona;
  collapsed: boolean;
  onNavigate?: () => void;
}) {
  const t = useTranslations("nav.sidebar");
  const router = useRouter();
  const api = useApi();
  const { requestCall } = useCallSession();

  const handleCall = useCallback(() => {
    void (async () => {
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
      // "switch" opens the end-and-switch confirm, which navigates on
      // confirm — don't navigate here (V7 D-V7-4, the one-call rule).
      if (requestCall(target) !== "switch") {
        router.push(`/chat/${conv.data.id}/voice`);
      }
    })();
  }, [api, persona, requestCall, router]);

  const handleChat = useCallback(() => {
    void startChat(persona.id);
  }, [persona.id]);

  const gesture = usePressSwipeGesture<HTMLAnchorElement>({
    onCommitUp: handleCall,
    onCommitDown: handleChat,
  });

  return (
    <li>
      <Tooltip>
        <TooltipTrigger
          render={
            <Link
              ref={gesture.elementRef}
              href={`/personas/${persona.id}`}
              onClick={(event) => {
                gesture.handlers.onClick(event);
                // An armed gesture (commit OR cancel) already did its own
                // thing — only a plain, un-armed tap should ALSO run the
                // caller's onNavigate (e.g. closing the mobile sheet),
                // matching "today's click, byte-identical".
                if (!event.defaultPrevented) onNavigate?.();
              }}
              onPointerDown={gesture.handlers.onPointerDown}
              onContextMenu={(event) => {
                // Suppress the OS long-press context menu / iOS "peek" while
                // swiping — it would otherwise fight the live preview for
                // the same drag. (Unverified without a real device — see
                // the fix's evidence note.)
                if (gesture.armed) event.preventDefault();
              }}
              aria-label={persona.name}
              className={cn(
                "block touch-none rounded-full ring-offset-background transition-[transform,box-shadow] duration-[var(--motion-duration-fast)] ease-[var(--motion-ease-standard)] outline-none hover:-translate-y-0.5 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 motion-reduce:transition-none motion-reduce:hover:translate-y-0",
                "[-webkit-touch-callout:none]",
              )}
            />
          }
        >
          <PersonaAvatar persona={persona} size="md" />
        </TooltipTrigger>
        <TooltipContent side="right">{persona.name}</TooltipContent>
      </Tooltip>
      <PressSwipePreview
        armed={gesture.armed}
        highlight={gesture.highlight}
        progress={gesture.progress}
        anchorRect={gesture.anchorRect}
        collapsed={collapsed}
        up={{
          icon: <Phone className="size-3.5" aria-hidden="true" />,
          label: t("callPersona", { name: persona.name }),
        }}
        down={{
          icon: <MessageSquare className="size-3.5" aria-hidden="true" />,
          label: t("newChat"),
        }}
      />
    </li>
  );
}

/**
 * "All chats (N)" — the MESSAGES section's link to the full /conversations
 * page (R9-009: the Conversations nav row folded into this section; the route
 * and page are unchanged). N is the honest owner total from /v1/me/nav-counts
 * (the same figure the removed nav row's badge showed); at 0 the count is
 * hidden (the badge zero-hidden convention).
 *
 * Expanded: a caption link in the section-header row. Collapsed (icon rail):
 * R9-032 — this is the SOLE occupant of the MESSAGES section (the avatar
 * preview list is scroll-hostile in the narrow rail, per owner ruling, so it
 * no longer renders there — see sidebar.tsx). Renders as a tooltip-labelled
 * icon button carrying a visible live count badge (same honest total, same
 * nav-counts feed, zero-hidden) — /conversations stays reachable in rail mode.
 */
export function AllChatsLink({
  count,
  collapsed = false,
  onNavigate,
}: {
  count: number;
  collapsed?: boolean;
  onNavigate?: () => void;
}) {
  const t = useTranslations("nav.sidebar");
  const label = t("allChats", { count });

  if (collapsed) {
    return (
      // R9-036 rider: the button sat flush against the top of the MESSAGES
      // section's flexible (mostly-empty, in collapsed mode) region, reading
      // as "slightly too high" against the PERSONAS rail above it. A tiny
      // `pt-1` (4px — the same small increment CollapseToggle/NotificationBell
      // already use as their own icon-row gap in the header, sidebar.tsx)
      // nudges it down without materially re-centering the whole section.
      <div className="flex justify-center pt-1 pb-1">
        <Tooltip>
          <TooltipTrigger
            render={
              <Link
                href="/conversations"
                onClick={onNavigate}
                aria-label={label}
                data-slot="sidebar-all-chats-collapsed"
                className="relative grid size-9 place-items-center rounded-md text-muted-foreground outline-none transition-colors duration-[var(--motion-duration-fast)] hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground focus-visible:ring-2 focus-visible:ring-ring motion-reduce:transition-none"
              />
            }
          >
            <MessagesSquare className="size-4" />
            {/* R9-032: a visible live count badge (mirrors NotificationBell's
                unread-dot pattern) — aria-hidden because the Link's aria-label
                above already carries the full count via the same i18n string. */}
            {count > 0 ? (
              <span
                aria-hidden
                data-slot="sidebar-all-chats-count"
                className="type-caption -top-0.5 -right-0.5 absolute flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-1 font-medium text-primary-foreground"
              >
                {count}
              </span>
            ) : null}
          </TooltipTrigger>
          <TooltipContent side="right">{label}</TooltipContent>
        </Tooltip>
      </div>
    );
  }

  return (
    <Link
      href="/conversations"
      onClick={onNavigate}
      data-slot="sidebar-all-chats"
      className="rounded-sm type-caption normal-case tracking-normal text-muted-foreground tabular-nums outline-none transition-colors duration-[var(--motion-duration-fast)] hover:text-sidebar-accent-foreground focus-visible:ring-2 focus-visible:ring-ring motion-reduce:transition-none"
    >
      {label}
    </Link>
  );
}

/**
 * MESSAGES — the chat-app conversation list. This is the flexible, growing,
 * scrolling region of the sidebar (the parent caps its height and the
 * <ScrollArea> wrapper scrolls). Each row: persona avatar + a title line
 * (the persona name) + a one-line brief (the conversation title, truncated).
 *
 * `GET /v1/conversations` exposes no last-message author or preview, so the
 * brief is the conversation title and the title line is the persona name —
 * see `sidebar-data.ts` for the rationale. Collapsed mode shows avatar-only
 * rows with the persona name in a tooltip.
 */
export function MessagesList({
  conversations,
  collapsed,
  onNavigate,
}: {
  conversations: readonly SidebarConversation[];
  collapsed: boolean;
  onNavigate?: () => void;
}) {
  const t = useTranslations("nav.sidebar");
  const format = useFormatter();
  const pathname = usePathname();

  if (conversations.length === 0) {
    return collapsed ? null : (
      <p className="px-2 py-1.5 type-caption text-muted-foreground">
        {t("messagesEmpty")}
      </p>
    );
  }

  return (
    <ul className="flex flex-col gap-0.5" data-slot="sidebar-messages-list">
      {conversations.map((c) => {
        const href = `/chat/${c.id}`;
        const active = pathname === href;
        const title = c.persona ? c.persona.name : t("unknownPersona");
        const brief = c.title?.trim() || t("untitled");

        if (collapsed) {
          return (
            <li key={c.id} className="flex justify-center">
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Link
                      href={href}
                      onClick={onNavigate}
                      aria-label={`${title} — ${brief}`}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "block rounded-full p-0.5 outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
                        active && "ring-2 ring-sidebar-ring",
                      )}
                    />
                  }
                >
                  {c.persona ? (
                    <PersonaAvatar persona={c.persona} size="sm" />
                  ) : (
                    <span
                      className="block size-6 rounded-full bg-muted"
                      aria-hidden
                    />
                  )}
                </TooltipTrigger>
                <TooltipContent side="right">{title}</TooltipContent>
              </Tooltip>
            </li>
          );
        }

        return (
          <li key={c.id}>
            <Link
              href={href}
              onClick={onNavigate}
              aria-current={active ? "page" : undefined}
              title={brief}
              // Spec 35 D-35-13: the active conversation row carries the
              // persona's identity colour as a 2px left border (the identity
              // spine). personaIdentityStyle sets --v-id on the row; inactive
              // rows keep a transparent border so layout doesn't shift.
              style={{
                ...(c.persona ? personaIdentityStyle(c.persona) : {}),
                borderLeftColor: active ? "var(--v-id)" : "transparent",
              }}
              className={cn(
                "group/msg flex items-center gap-2.5 rounded-md border-l-2 px-2 py-1.5 outline-none transition-colors duration-[var(--motion-duration-fast)] ease-[var(--motion-ease-standard)] focus-visible:ring-2 focus-visible:ring-ring motion-reduce:transition-none",
                active
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "hover:bg-sidebar-accent/60",
              )}
            >
              {c.persona ? (
                <PersonaAvatar
                  persona={c.persona}
                  size="sm"
                  className="shrink-0"
                />
              ) : (
                <span
                  className="size-6 shrink-0 rounded-full bg-muted"
                  aria-hidden
                />
              )}
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="flex items-baseline justify-between gap-2">
                  <span
                    className={cn(
                      "type-ui truncate font-medium",
                      !active && "text-sidebar-foreground",
                    )}
                  >
                    {title}
                  </span>
                  <span className="flex shrink-0 items-center gap-1.5">
                    {/* Spec P1 D-P1-v7-indicator: a "working" pulse while this
                        conversation has an in-progress detached turn. */}
                    <ActiveChatIndicator
                      conversationId={c.id}
                      personaName={c.persona?.name}
                    />
                    <RelativeTime iso={c.updated_at} format={format} />
                  </span>
                </span>
                <span className="truncate type-caption normal-case tracking-normal text-muted-foreground">
                  {brief}
                </span>
              </span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * CALLS — a compact list of recent voice calls (Spec V9). Each row: persona
 * avatar + name + the call duration (or a generic "Call" label while live /
 * unknown) + the relative time the call started. The row links to
 * `/chat/{conversationId}` — the saved transcript, since the spoken turns
 * persist as conversation messages (V9-D-1/D-2) and the chat page renders them.
 * The full, paginated history lives at `/calls`. Collapsed mode shows
 * avatar-only rows with the persona name in a tooltip (mirrors MessagesList).
 */
export function CallsList({
  calls,
  collapsed,
  onNavigate,
}: {
  calls: readonly SidebarCall[];
  collapsed: boolean;
  onNavigate?: () => void;
}) {
  const t = useTranslations("nav.sidebar");
  const format = useFormatter();
  const pathname = usePathname();

  if (calls.length === 0) {
    return collapsed ? null : (
      <p className="px-2 py-1.5 type-caption text-muted-foreground">
        {t("callsEmpty")}
      </p>
    );
  }

  return (
    <ul className="flex flex-col gap-0.5" data-slot="sidebar-calls-list">
      {calls.map((c) => {
        const href = `/chat/${c.conversationId}`;
        const active = pathname === href;
        const title = c.persona ? c.persona.name : t("unknownPersona");
        const brief = formatCallDuration(c.durationS) ?? t("callOngoing");

        if (collapsed) {
          return (
            <li key={c.callId} className="flex justify-center">
              <Tooltip>
                <TooltipTrigger
                  render={
                    <Link
                      href={href}
                      onClick={onNavigate}
                      aria-label={`${title} — ${brief}`}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "block rounded-full p-0.5 outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
                        active && "ring-2 ring-sidebar-ring",
                      )}
                    />
                  }
                >
                  {c.persona ? (
                    <PersonaAvatar persona={c.persona} size="sm" />
                  ) : (
                    <span
                      className="block size-6 rounded-full bg-muted"
                      aria-hidden
                    />
                  )}
                </TooltipTrigger>
                <TooltipContent side="right">{title}</TooltipContent>
              </Tooltip>
            </li>
          );
        }

        return (
          <li key={c.callId}>
            <Link
              href={href}
              onClick={onNavigate}
              aria-current={active ? "page" : undefined}
              title={`${title} — ${brief}`}
              style={{
                ...(c.persona ? personaIdentityStyle(c.persona) : {}),
                borderLeftColor: active ? "var(--v-id)" : "transparent",
              }}
              className={cn(
                "group/call flex items-center gap-2.5 rounded-md border-l-2 px-2 py-1.5 outline-none transition-colors duration-[var(--motion-duration-fast)] ease-[var(--motion-ease-standard)] focus-visible:ring-2 focus-visible:ring-ring motion-reduce:transition-none",
                active
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "hover:bg-sidebar-accent/60",
              )}
            >
              {c.persona ? (
                <PersonaAvatar
                  persona={c.persona}
                  size="sm"
                  className="shrink-0"
                />
              ) : (
                <span
                  className="size-6 shrink-0 rounded-full bg-muted"
                  aria-hidden
                />
              )}
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="flex items-baseline justify-between gap-2">
                  <span
                    className={cn(
                      "type-ui truncate font-medium",
                      !active && "text-sidebar-foreground",
                    )}
                  >
                    {title}
                  </span>
                  <RelativeTime iso={c.startedAt} format={format} />
                </span>
                <span className="flex items-center gap-1 truncate type-caption normal-case tracking-normal text-muted-foreground">
                  <Phone className="size-3 shrink-0" aria-hidden />
                  {brief}
                </span>
              </span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * A relative timestamp ("2h ago"), rendered client-only after mount.
 *
 * `relativeTime` depends on "now", which differs between the server render and
 * client hydration → a guaranteed hydration mismatch if rendered eagerly. We
 * render a stable empty `<time>` on the server + first paint (the absolute ISO
 * is always available to assistive tech via `dateTime`), then fill the label in
 * a post-hydration effect. This keeps the row's brief line authoritative while
 * the timestamp is a progressive enhancement.
 */
function RelativeTime({
  iso,
  format,
}: {
  iso: string;
  format: ReturnType<typeof useFormatter>;
}) {
  const [label, setLabel] = useState<string>("");
  useEffect(() => {
    setLabel(
      format.relativeTime(new Date(iso), { now: new Date(), style: "narrow" }),
    );
  }, [iso, format]);
  return (
    <time
      dateTime={iso}
      suppressHydrationWarning
      className="shrink-0 type-caption text-muted-foreground"
    >
      {label}
    </time>
  );
}
