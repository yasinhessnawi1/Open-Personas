"use client";

/**
 * Spec A11 — headless: a background `message.delivered` re-orders the conversation
 * list. The sidebar is server-rendered (`fetchSidebarData`), so a soft
 * `router.refresh()` re-runs the server components to bring the just-delivered
 * conversation to the top — client state (the open chat's `useChat` messages) is
 * preserved across a soft refresh, so the stream/scroll is untouched.
 *
 * Only `message.delivered` reorders the list (a new message changed recency);
 * `notification.created` is the bell's concern, not the list. Mounted once inside
 * {@link MeEventsProvider}.
 *
 * Spec A6 (W8) — it ALSO bridges the A11 `task.updated` frame onto the W8
 * {@link emitTaskSignal} bus: a background task transition (completed / cancelled /
 * waiting_on_user / budget_paused) pings the Review/Tasks/Approvals surfaces, which
 * refetch their durable data (never trusting the pushed state). The router already
 * deduped the frame by id, so a monotonic local counter is a safe advance-only
 * signal id. This is the merge-back wiring that makes the W8 seam live.
 */

import { useRouter } from "next/navigation";
import { useCallback, useRef } from "react";
import { useMeEvent } from "@/components/providers/me-events-provider";
import { emitTaskSignal } from "@/lib/task-signal";

export function MeLiveRefresh() {
  const router = useRouter();
  useMeEvent(
    "message.delivered",
    useCallback(() => router.refresh(), [router]),
  );

  // task.updated → the W8 refetch bus. The `data` carries `{ task_id, state }`; the
  // signal id is a local monotonic counter (the frame was already deduped upstream).
  const seq = useRef(0);
  useMeEvent(
    "task.updated",
    useCallback((data: Record<string, unknown>) => {
      seq.current += 1;
      const taskId = typeof data.task_id === "string" ? data.task_id : null;
      emitTaskSignal({ id: `task:${seq.current}`, taskId });
    }, []),
  );
  return null;
}
