"use client";

/**
 * R9-038 — the sidebar rail's per-persona "working" signal.
 *
 * Investigation finding (see the fix evidence for the full trail): the raw
 * `task.updated` SSE frame carries only `{task_id, state}` — NO persona_id
 * (me-live-refresh.tsx) — so the live channel is a refetch trigger only,
 * never a state source (the A11 contract everywhere else in this app). The
 * nav-counts badge (`SidebarNavCounts.activeTasks`) is the same shape: an
 * honest total, no per-persona attribution. So this hook does NOT invent new
 * backend plumbing; it composes TWO client sources that already exist and are
 * already persona-attributed:
 *
 *  - {@link useActiveWork}'s `activeChats` (Spec P1 D-P1-v7-indicator): every
 *    conversation with an in-progress DETACHED chat turn, each tagged with
 *    `personaId` — polled independently of the current route (so a persona
 *    you've navigated away from still shows as working). This is the EXACT
 *    signal `ActiveChatIndicator` already renders per conversation row; this
 *    hook just re-keys it by persona instead of by conversation.
 *  - `GET /v1/tasks` (Spec A6 `fetchTasks`, the SAME durable list `TasksList`
 *    reads), filtered to `status === "progressing"` — a task that has taken
 *    at least one leg and is genuinely mid-flight. Deliberately narrower than
 *    the nav-counts' full non-terminal set: `just_created` (not started yet),
 *    `waiting_on_user` / `scheduled` (parked, blocked), and `paused` are
 *    real non-terminal states but NOT "live" right now — pulsing "working"
 *    over a task that is simply waiting on the owner would be dishonest.
 *    Refetched on the W8 `task.updated` signal bus (refetch-on-ping, never
 *    trust the pushed payload — A6-R-4), plus once on mount; no polling
 *    interval of its own (mirrors `TasksList`).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useAuth } from "@/auth";
import { fetchTasks } from "@/lib/api/tasks-client";
import { useTaskSignal } from "@/lib/task-signal";
import { useActiveWork } from "@/lib/work/active-work-context";

/** Task statuses that mean "genuinely mid-flight right now" (Spec A2/A6 IntrospectionStatus). */
const WORKING_TASK_STATUSES: ReadonlySet<string> = new Set(["progressing"]);

export function usePersonaWorkingIds(): ReadonlySet<string> {
  const { getToken } = useAuth();
  const { activeChats } = useActiveWork();
  const [taskPersonaIds, setTaskPersonaIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      const tasks = await fetchTasks(token);
      setTaskPersonaIds(
        new Set(
          tasks
            .filter((task) => WORKING_TASK_STATUSES.has(task.status))
            .map((task) => task.persona_id),
        ),
      );
    } catch {
      // Fail-soft (matches ActiveWorkProvider's own poll): a transient
      // failure just means no task-driven working state until the next
      // mount or task.updated ping — the ring never breaks the rail.
    }
  }, [getToken]);

  useEffect(() => {
    void load();
  }, [load]);

  // W8: refetch-on-ping, never trust the pushed payload (A6-R-4) — the same
  // seam TasksList itself subscribes to.
  useTaskSignal(() => void load());

  return useMemo(() => {
    const merged = new Set<string>(taskPersonaIds);
    for (const chat of activeChats) merged.add(chat.personaId);
    return merged;
  }, [activeChats, taskPersonaIds]);
}
