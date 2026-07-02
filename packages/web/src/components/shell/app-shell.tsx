/**
 * AppShell — the authenticated app frame: a rich, resizable/collapsible
 * desktop sidebar + a sticky header, with a mobile sheet at < md.
 *
 * Server component: it fetches the sidebar's recency-derived data once
 * (`fetchSidebarData`) and hands the serialisable result to the client
 * `<Sidebar>` (which owns resize/collapse + the section model) and to the
 * mobile `<MobileNav>` sheet. The header keeps the Clerk `<UserMenu />` +
 * `<ThemeToggle />` islands untouched (Phase-1 plumbing) — the account stays in
 * the header; Settings lives in the sidebar's pinned-bottom slot, not here.
 */

import type { ReactNode } from "react";
import { ToastProvider } from "@/components/patterns/toast";
import { NameNudge } from "@/components/profile/name-nudge";
import { ConfirmProvider } from "@/components/providers/confirm-provider";
import { NotificationProvider } from "@/components/providers/notification-provider";
import { ServerNotificationsProvider } from "@/components/providers/server-notifications-provider";
import { CommandPalette } from "@/components/shell/command-palette";
import { LowBalanceWatcher } from "@/components/shell/low-balance-watcher";
import { NotificationBell } from "@/components/shell/notification-bell";
import { Sidebar } from "@/components/shell/sidebar";
import { MiniCallBar } from "@/components/voice/mini-call-bar";
import { ResumeCallPrompt } from "@/components/voice/resume-call-prompt";
import { SwitchCallDialog } from "@/components/voice/switch-call-dialog";
import { ActiveWorkBar } from "@/components/work/active-work-bar";
import { cn } from "@/lib/utils";
import { CallSessionProvider } from "@/lib/voice/call-session-context";
import { ActiveWorkProvider } from "@/lib/work/active-work-context";
import { MobileNav } from "./mobile-nav";
import type { SidebarData } from "./sidebar-data";
import { fetchSidebarData } from "./sidebar-fetch";

export async function AppShell({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  const data = await fetchSidebarData();

  return (
    // Spec 35 clusters L + M: the global notification (D-35-10/11) + confirm
    // (D-35-12) systems wrap the shell so useNotify()/useConfirm() reach every
    // page. Both are client components; the server-rendered children pass
    // through as a prop (standard Next 16 server-child-of-client pattern, like
    // ThemeProvider in the root layout).
    //
    // Spec V7 D-V7-1: the app-level call session is hoisted here too — mounted
    // ONCE, above the routed pages, so a voice call survives in-app navigation.
    // CallSessionProvider is nested INSIDE Confirm/Notification so any descendant
    // (and the hoisted voice surfaces) can reach useConfirm()/useNotify(); those
    // providers must sit above any component that consumes them. AppShell stays
    // an async server component: each provider is a client boundary that takes
    // the server-rendered routes as `children`, so a call-state change re-renders
    // only the client subtree and never re-runs `fetchSidebarData`.
    // NOTE: SwitchCallDialog keeps its own state-driven modal (not useConfirm) —
    // its serialized end→start + post-confirm navigation are bound to the session's
    // `pendingSwitch`, which an imperative confirm() boolean can't express without
    // risking the "never two Rooms" guarantee.
    // HARD GUARD: the call's Room + <audio> + mic live inside CallSessionProvider,
    // never a route. The mini-bar (T2) renders inside it, bound to the session.
    <NotificationProvider>
      {/* Spec P6 (D4-e): the durable cross-device feed, polled once for the app;
          the bell renders it unioned with the client useNotify() feed. */}
      <ServerNotificationsProvider>
        <ConfirmProvider>
          <CallSessionProvider>
            {/* Spec P1 D-P1-v7-indicator: the chat/run "active work" session — the
              additive sibling of the voice CallSessionProvider (voice mechanics
              untouched). Tracks in-progress detached chat turns so the
              conversation row + the global ActiveWorkBar advertise resumable work. */}
            <ActiveWorkProvider>
              <div
                className={cn("flex min-h-svh", className)}
                data-slot="app-shell"
              >
                <Sidebar data={data} />
                <div className="flex min-w-0 flex-1 flex-col">
                  <ShellHeader data={data} />
                  <main
                    className="flex flex-1 flex-col"
                    data-slot="app-shell-main"
                  >
                    {children}
                  </main>
                </div>
                {/* F2 T23: single toast surface for the auth'd app. */}
                <ToastProvider />
                {/* Spec P6 (P6-D-6): low-balance-at-load — headless; reads
                /v1/me/credits once per session and warns via useNotify(). */}
                <LowBalanceWatcher />
                {/* Spec 35 D-35-14: the ⌘K command palette, mounted once for the app. */}
                <CommandPalette data={data} />
                {/* Spec K6 (T5): the optional "what should we call you?" nudge —
                shown only when our DB has no name for the caller; skippable, no
                dark pattern; dismissed for the session. */}
                <NameNudge />
                {/* Spec V7 D-V7-2: the persistent mini call-bar — hidden until a call is
                active; binds the hoisted session, never owns a Room. */}
                <MiniCallBar />
                {/* Spec P1 D-P1-v7-indicator: the global "working — return to it" bar,
                alongside the call pill (additive). Hidden unless a chat turn runs. */}
                <ActiveWorkBar />
                {/* Spec V7 D-V7-4: the end-and-switch confirm — shown only when a call is
                requested while a different one is active. */}
                <SwitchCallDialog />
                {/* Spec V7 D-V7-3: the resume-after-reload prompt — shown only when a
                recent call is found in sessionStorage on load. */}
                <ResumeCallPrompt />
              </div>
            </ActiveWorkProvider>
          </CallSessionProvider>
        </ConfirmProvider>
      </ServerNotificationsProvider>
    </NotificationProvider>
  );
}

/**
 * Sticky header — the mobile-nav trigger (the account + theme controls moved
 * into the sidebar account footer, Spec 35 D-35-16). The backdrop-blur +
 * bg-background/85 reads as the F1 paper-on-paper lift; `shadow-[var(--elevation-1)]`
 * is the explicit resting elevation token. On desktop the rail owns navigation,
 * so the header is a thin sticky bar reserved for future breadcrumb/actions.
 */
function ShellHeader({ data }: { data: SidebarData }) {
  return (
    <header
      className="sticky top-0 z-20 flex h-14 items-center gap-2 border-b bg-background/85 px-4 shadow-[var(--elevation-1)] backdrop-blur md:hidden"
      data-slot="app-shell-header"
    >
      <MobileNav data={data} />
      <div className="flex-1" />
      {/* Spec 35 cluster L: the notification bell — reachable on mobile without
       * opening the nav sheet (the sidebar header carries it on desktop). */}
      <NotificationBell />
    </header>
  );
}
