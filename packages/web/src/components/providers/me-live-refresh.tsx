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
 */

import { useRouter } from "next/navigation";
import { useCallback } from "react";
import { useMeEvent } from "@/components/providers/me-events-provider";

export function MeLiveRefresh() {
  const router = useRouter();
  useMeEvent(
    "message.delivered",
    useCallback(() => router.refresh(), [router]),
  );
  return null;
}
