"use client";

import { ListTodo } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/auth";
import { Stack } from "@/components/layout";
import { EmptyState } from "@/components/patterns/empty-state";
import { SkeletonBlock } from "@/components/patterns/loading";
import { useToast } from "@/components/patterns/toast";
import {
  cancelTask,
  fetchTasks,
  type TaskSummary,
} from "@/lib/api/tasks-client";
import { useSidebarRefresh } from "@/lib/hooks/use-sidebar-refresh";
import { useTaskSignal } from "@/lib/task-signal";

import { TaskRow } from "./task-row";

/** persona_id → display name (server-fetched; falls back to the id). */
export type PersonaNames = Record<string, string>;

/** Waiting-first ordering (A6-D-2): waiting_on_user → stuck/failed → active → recent-terminal. */
const ORDER: Record<string, number> = {
  waiting_on_user: 0,
  failed: 1,
  paused: 2,
  progressing: 3,
  just_created: 4,
  scheduled: 5,
  completed: 6,
  cancelled: 7,
};

export function ordered(tasks: TaskSummary[]): TaskSummary[] {
  return [...tasks].sort((a, b) => {
    const rank = (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9);
    if (rank !== 0) return rank;
    return b.updated_at.localeCompare(a.updated_at); // newest-touched first within a tier
  });
}

export function TasksList({ personaNames }: { personaNames: PersonaNames }) {
  const t = useTranslations("taskList");
  const { getToken } = useAuth();
  const refreshSidebar = useSidebarRefresh();
  const toast = useToast();
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    try {
      setTasks(await fetchTasks(await getToken()));
    } catch {
      setTasks([]);
      toast.error(t("loadFailed"));
    }
  }, [getToken, toast, t]);

  useEffect(() => {
    void load();
  }, [load]);

  // W8: refetch the durable list on a task.updated signal (refetch-not-trust, A6-R-4).
  useTaskSignal(() => void load());

  const onCancel = useCallback(
    async (taskId: string) => {
      setBusy((b) => ({ ...b, [taskId]: true }));
      try {
        const result = await cancelTask(await getToken(), taskId);
        // reflect the durable post-state (A6-D-3) — a terminal task simply updates in place.
        setTasks((list) =>
          (list ?? []).map((task) =>
            task.task_id === taskId
              ? { ...task, status: result.status, stuck_cause: null }
              : task,
          ),
        );
        // R9-012: the shared sidebar-refresh seam — the Activity badge counts
        // non-terminal tasks; a cancel moves it (soft refresh, list state kept).
        refreshSidebar();
      } catch {
        toast.error(t("cancelFailed"));
      } finally {
        setBusy((b) => ({ ...b, [taskId]: false }));
      }
    },
    [getToken, toast, t, refreshSidebar],
  );

  if (tasks === null) {
    return (
      <Stack gap={3}>
        <SkeletonBlock className="h-24" />
        <SkeletonBlock className="h-24" />
      </Stack>
    );
  }

  if (tasks.length === 0) {
    return (
      <EmptyState
        icon={<ListTodo className="size-6" />}
        title={t("emptyTitle")}
        description={t("emptyBody")}
      />
    );
  }

  return (
    <Stack gap={3}>
      {ordered(tasks).map((task) => (
        <TaskRow
          key={task.task_id}
          task={task}
          personaName={personaNames[task.persona_id] ?? task.persona_id}
          busy={busy[task.task_id] ?? false}
          onCancel={() => onCancel(task.task_id)}
        />
      ))}
    </Stack>
  );
}
