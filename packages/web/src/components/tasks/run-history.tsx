"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { RunStatusBadge } from "@/components/runs/run-status-badge";
import { buttonVariants } from "@/components/ui/button";
import type { TaskRun } from "@/lib/api/tasks-client";
import type { RunStatus } from "@/lib/run";
import { cn } from "@/lib/utils";

/**
 * Spec W1 (D-W1-3): the task detail is the home of its run history.
 *
 * A run is an execution of a task, so a dispatched one-off stays reachable here after the
 * user navigates away: each run is a row with its status and a link into the step-level
 * viewer. The list is durable state (fetched, never inferred); the parent refetches on the
 * task.updated signal and, while a run is live, on a short poll, so a status flips without
 * a reload. No steps are loaded here; the viewer owns those.
 *
 * R9-158: a run stopped early says why (the user's pause or cancel, a limit, a restart, an
 * approval), rather than a bare "cancelled"; and while a leg is queued but no run is live
 * yet (the moment right after Resume), a "Starting" row leads the list, so the old stopped
 * run is never presented as the latest word on a task that is already moving again.
 */
export function RunHistory({
  runs,
  legQueued = false,
}: {
  runs: TaskRun[];
  legQueued?: boolean;
}) {
  const t = useTranslations("taskDetail");
  const starting = legQueued && !runs.some((run) => run.status === "running");
  if (runs.length === 0 && !starting) {
    return (
      <p
        className="type-ui text-muted-foreground"
        data-slot="run-history-empty"
      >
        {t("runsEmpty")}
      </p>
    );
  }
  return (
    <ol className="flex flex-col gap-2" data-slot="run-history">
      {starting ? (
        <li
          className="rounded-lg border border-dashed px-3 py-2"
          data-slot="run-history-starting"
        >
          <output className="type-caption text-muted-foreground">
            {t("runStarting")}
          </output>
        </li>
      ) : null}
      {runs.map((run) => (
        <li
          key={run.id}
          className="flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3 py-2"
          data-slot="run-history-row"
          data-status={run.status}
        >
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <RunStatusBadge status={run.status as RunStatus} />
              <span className="type-caption text-muted-foreground">
                {run.status === "running"
                  ? t("runLive")
                  : t("runStarted", { time: formatStarted(run.started_at) })}
              </span>
            </div>
            {isStopReason(run.stop_reason) ? (
              <span
                className="type-caption text-muted-foreground"
                data-slot="run-history-stop-reason"
              >
                {t(`stopReason.${run.stop_reason}`)}
              </span>
            ) : null}
          </div>
          <Link
            href={`/runs/${encodeURIComponent(run.id)}`}
            className={cn(
              buttonVariants({ variant: "link", size: "sm" }),
              "-mr-2",
            )}
            data-slot="run-history-open"
          >
            {t("openRun")}
          </Link>
        </li>
      ))}
    </ol>
  );
}

/** The reasons the api records (``RunStopReason``); anything else shows no reason line. */
const STOP_REASONS = [
  "paused",
  "cancelled",
  "budget",
  "wall_clock",
  "steps",
  "drain",
  "approval",
] as const;

type StopReason = (typeof STOP_REASONS)[number];

function isStopReason(value: string | null | undefined): value is StopReason {
  return (STOP_REASONS as readonly string[]).includes(value ?? "");
}

/** A short, local time for the row; the viewer carries the precise timestamps. */
function formatStarted(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
