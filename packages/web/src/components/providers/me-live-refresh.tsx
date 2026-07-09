"use client";

/**
 * Spec A11 / R9-012 — headless: the live channel + window focus keep the
 * server-rendered sidebar (lists AND nav-count badges) honest without a reload.
 *
 * The sidebar is server-rendered (`fetchSidebarData`), so a soft
 * `router.refresh()` re-runs the server components — client state (the open
 * chat's `useChat` messages, scroll) is preserved across a soft refresh. All
 * refreshes here go through the R9-012 debounced seam
 * ({@link useDebouncedSidebarRefresh}): an event burst (a task fanning out
 * transitions, a delivery flurry) coalesces into ONE trailing refresh.
 *
 * Trigger classes (R9-012):
 * - `message.delivered` — a background message re-orders the conversation list
 *   (the original A11 wiring).
 * - `task.updated` — the Activity badge counts non-terminal tasks; a background
 *   transition moves it. ALSO bridges onto the W8 {@link emitTaskSignal} bus
 *   (Spec A6): Review/Tasks/Approvals refetch their durable data (never
 *   trusting the pushed state).
 * - `notification.created` — schedule fires etc. move badges (the bell itself
 *   is ServerNotificationsProvider's concern; this is the sidebar's).
 * - `sidebar.changed` — the generic cross-device ping from the mutation routes
 *   (persona/conversation create+delete, schedule create).
 * - `resync` — the channel could not replay; refetch everything.
 * - window focus — the fail-soft floor: with the channel down, a returning tab
 *   still catches up on focus (never broken, never spinning).
 *
 * Mounted once inside {@link MeEventsProvider}.
 */

import { useCallback, useEffect, useRef } from "react";
import { useMeEvent } from "@/components/providers/me-events-provider";
import { useDebouncedSidebarRefresh } from "@/lib/hooks/use-sidebar-refresh";
import { emitTaskSignal } from "@/lib/task-signal";

export function MeLiveRefresh() {
  const refresh = useDebouncedSidebarRefresh();
  const onPing = useCallback(() => refresh(), [refresh]);

  useMeEvent("message.delivered", onPing);
  useMeEvent("notification.created", onPing);
  useMeEvent("sidebar.changed", onPing);
  useMeEvent("resync", onPing);

  // task.updated → the W8 refetch bus + a sidebar refresh (the Activity badge).
  // The `data` carries `{ task_id, state }`; the signal id is a local monotonic
  // counter (the frame was already deduped upstream by the channel router).
  const seq = useRef(0);
  useMeEvent(
    "task.updated",
    useCallback(
      (data: Record<string, unknown>) => {
        seq.current += 1;
        const taskId = typeof data.task_id === "string" ? data.task_id : null;
        emitTaskSignal({ id: `task:${seq.current}`, taskId });
        refresh();
      },
      [refresh],
    ),
  );

  // Fail-soft floor (R9-012): channel down ⇒ the sidebar still catches up when
  // the tab regains focus (the debounce also spares a rapid alt-tab storm).
  useEffect(() => {
    const onFocus = () => refresh();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refresh]);

  return null;
}
