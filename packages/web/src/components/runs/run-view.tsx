"use client";

import { X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { Stack } from "@/components/layout";
import { buttonVariants } from "@/components/ui/button";
import type { RunStatusResponse } from "@/lib/api";
import { useRun } from "@/lib/hooks/use-run";
import { cn } from "@/lib/utils";
import { RunStatusBadge } from "./run-status-badge";
import { RunTimeline } from "./run-timeline";

/**
 * Spec F2 T30 — RunView (rebuilt presentation).
 *
 * The live run viewer (T07-era client component). Server renders the
 * persona/task brief; this component owns the streaming timeline, status,
 * cancel, and ask-user answers.
 *
 * DO NOT TOUCH (per audit.md §runs.plumbing):
 *   - `useRun(runId, initial)` hook (owns SSE consumption, polymorphic
 *     `runs.steps` reduction via `runViewFromEvents`, /respond, /cancel);
 *   - cancel state machine.
 *
 * REPLACED:
 *   - run-tier label `font-mono text-[0.65rem] tracking-wide uppercase`
 *     (scaffold line 49 — the named text-[0.65rem] legacy) → `.type-caption font-mono uppercase`;
 *   - outer `flex flex-col gap-5` → T20 `<Stack gap={5}>`;
 *   - run-level error `text-sm text-destructive` → `.type-ui text-destructive`
 *     with `role="alert"` for assertive announcement.
 */
export function RunView({
  runId,
  initial,
}: {
  runId: string;
  initial: RunStatusResponse;
}) {
  // Spec W1 (D-W1-4 / D-W1-28): a run that belongs to a task takes its answers on the task
  // page (T6's reply door), never through the retired in-process respond queue.
  const answerHref = initial.task_id
    ? `/activity/tasks/${encodeURIComponent(initial.task_id)}`
    : undefined;
  const t = useTranslations("runs");
  const { view, respond, cancel } = useRun(runId, initial);
  const [cancelling, setCancelling] = useState(false);

  async function onCancel() {
    if (cancelling) return;
    setCancelling(true);
    try {
      await cancel();
    } catch {
      // Cancel is best-effort; the timeline reconciles from the next reload.
    } finally {
      setCancelling(false);
    }
  }

  const runLevelError =
    view.error !== undefined && view.steps.every((s) => s.error === undefined);

  return (
    <Stack gap={5} data-slot="run-view">
      <div
        className="flex items-center justify-between gap-3"
        data-slot="run-view-header"
      >
        <div className="flex items-center gap-2">
          <RunStatusBadge status={view.status} />
          {view.tier ? (
            <span
              title={t("tierLabel", { tier: view.tier })}
              className="type-caption font-mono text-muted-foreground uppercase"
              data-slot="run-view-tier"
            >
              {view.tier}
            </span>
          ) : null}
        </div>
        {view.status === "running" ? (
          <button
            type="button"
            onClick={() => void onCancel()}
            disabled={cancelling}
            className={cn(
              buttonVariants({ variant: "outline", size: "sm" }),
              "gap-1.5",
            )}
            data-slot="run-view-cancel"
          >
            <X className="size-3.5" aria-hidden="true" />
            {t("cancel")}
          </button>
        ) : null}
      </div>

      <RunTimeline
        view={view}
        onAnswer={respond}
        personaId={initial.persona_id}
        answerHref={answerHref}
      />

      {runLevelError ? (
        <p
          className="type-ui text-destructive"
          role="alert"
          data-slot="run-view-error"
        >
          {view.error}
        </p>
      ) : null}
    </Stack>
  );
}
