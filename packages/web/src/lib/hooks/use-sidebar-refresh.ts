"use client";

/**
 * R9-012 — the ONE client seam that keeps the server-rendered sidebar honest.
 *
 * The app-router layout cache serves a stale sidebar after a same-tab mutation
 * (the shell's `fetchSidebarData` ran at the last server render). Every
 * mutating flow that moves the sidebar's lists or badges — persona
 * create/delete, conversation delete, schedule create, task cancel, call end —
 * calls {@link useSidebarRefresh}'s callback instead of hand-rolling its own
 * `router.refresh()`: a soft refresh re-runs the server components (layout
 * included, so the sidebar re-resolves) while PRESERVING client state (the
 * open chat's messages, form inputs — the Next 16 soft-refresh contract, the
 * MeLiveRefresh precedent). Conversation *create* needs no call site: it is a
 * server action (`startChat`/`startVoice`), and a completed server action
 * already invalidates the client router cache.
 *
 * {@link useDebouncedSidebarRefresh} is the burst-safe variant for the live
 * channel (MeLiveRefresh): a flurry of `task.updated`/`notification.created`
 * frames coalesces into ONE trailing refresh instead of a refresh storm.
 */

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef } from "react";

/** Trailing debounce for event-driven refreshes (R9-012: one refresh per burst). */
export const SIDEBAR_REFRESH_DEBOUNCE_MS = 1_500;

/**
 * The immediate seam for user-initiated mutations: returns a stable callback
 * that soft-refreshes the route tree (server components re-run; client state
 * preserved).
 */
export function useSidebarRefresh(): () => void {
  const router = useRouter();
  return useCallback(() => router.refresh(), [router]);
}

/**
 * The debounced seam for background events: trailing-edge, so an event burst
 * (a task fanning out transitions, a batch of deliveries) causes exactly one
 * refresh after the burst settles. The pending timer is cleared on unmount.
 */
export function useDebouncedSidebarRefresh(
  delayMs: number = SIDEBAR_REFRESH_DEBOUNCE_MS,
): () => void {
  const router = useRouter();
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );

  return useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      timer.current = null;
      router.refresh();
    }, delayMs);
  }, [router, delayMs]);
}
