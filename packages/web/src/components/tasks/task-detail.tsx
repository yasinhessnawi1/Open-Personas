"use client";

import { ChevronDown } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/auth";
import { Stack } from "@/components/layout";
import { SkeletonBlock } from "@/components/patterns/loading";
import { useToast } from "@/components/patterns/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  cancelTask,
  extendBudget,
  getTask,
  pauseTask,
  resumeTask,
  type TaskDetail as TaskDetailData,
} from "@/lib/api/tasks-client";
import { personaIdentityStyle } from "@/lib/persona-identity";
import { useTaskSignal } from "@/lib/task-signal";
import { cn } from "@/lib/utils";

import { InitiativeDialControl } from "./initiative-dial-control";
import { TaskReschedule } from "./task-reschedule";
import { kr } from "./task-row";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

function statusVariant(
  status: string,
): "outline" | "secondary" | "destructive" {
  if (status === "failed") return "destructive";
  if (status === "waiting_on_user") return "outline";
  return "secondary";
}

export function TaskDetail({
  taskId,
  personaNames,
}: {
  taskId: string;
  personaNames: Record<string, string>;
}) {
  const t = useTranslations("taskDetail");
  const { getToken } = useAuth();
  const toast = useToast();
  const [detail, setDetail] = useState<TaskDetailData | null | "error">(null);
  const [busy, setBusy] = useState(false);
  const [reflection, setReflection] = useState<string | null>(null);
  const [showQuestions, setShowQuestions] = useState(false);
  const [extendKr, setExtendKr] = useState("");

  const load = useCallback(async () => {
    try {
      setDetail(await getTask(await getToken(), taskId));
    } catch {
      setDetail("error");
    }
  }, [getToken, taskId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Every command reflects the DURABLE post-state (A6-R-4): refetch, never trust the pushed state.
  const refetch = useCallback(async () => {
    try {
      setDetail(await getTask(await getToken(), taskId));
    } catch {
      /* keep the last-known detail; the reflection still shows */
    }
  }, [getToken, taskId]);

  // W8: a task.updated signal refetches THIS task only when it's the one that changed (targeted).
  useTaskSignal((signal) => {
    if (signal.taskId === taskId) void refetch();
  });

  const runCommand = useCallback(
    async (
      verb: "pause" | "resume" | "cancel",
      fn: (
        token: string | null,
        id: string,
      ) => Promise<{ changed: boolean; note: string }>,
    ) => {
      setBusy(true);
      try {
        const result = await fn(await getToken(), taskId);
        // honest calm reflection: the server note wins (already-X / owner-paused / in-flight),
        // else a plain confirm for a real change.
        setReflection(result.note || t(`reflect.${verb}`));
        await refetch();
      } catch {
        toast.error(t("commandFailed"));
      } finally {
        setBusy(false);
      }
    },
    [getToken, taskId, refetch, toast, t],
  );

  const doExtend = useCallback(async () => {
    const amount = Math.round(Number(extendKr) * 10_000); // kr → micros
    if (!Number.isFinite(amount) || amount <= 0) return;
    setBusy(true);
    try {
      const r = await extendBudget(await getToken(), taskId, amount);
      setReflection(
        r.applied
          ? t("budgetExtended", {
              from: kr(r.old_cap_micros),
              to: kr(r.new_cap_micros),
            })
          : r.note,
      );
      setExtendKr("");
      await refetch();
    } catch {
      toast.error(t("commandFailed"));
    } finally {
      setBusy(false);
    }
  }, [extendKr, getToken, taskId, refetch, toast, t]);

  if (detail === null) return <SkeletonBlock className="h-64" />;
  if (detail === "error") {
    return <p className="type-body text-muted-foreground">{t("loadFailed")}</p>;
  }

  const terminal = TERMINAL.has(detail.status);
  const personaName = personaNames[detail.persona_id] ?? detail.persona_id;

  return (
    <Stack gap={4}>
      {/* header */}
      <div
        className="flex flex-col gap-2"
        style={personaIdentityStyle({ id: detail.persona_id })}
      >
        <div className="flex items-center gap-2">
          <span
            aria-hidden="true"
            className="size-2.5 rounded-[3px]"
            style={{ background: "var(--v-id)" }}
          />
          <span className="text-sm font-medium">{personaName}</span>
          <Badge
            variant={statusVariant(detail.status)}
            className={cn(
              detail.status === "waiting_on_user" &&
                "text-amber-600 dark:text-amber-500",
            )}
          >
            {t(`status.${detail.status}`)}
          </Badge>
        </div>
        <h2 className="type-heading">{detail.goal}</h2>
        {detail.scope ? (
          <p className="type-body text-muted-foreground">{detail.scope}</p>
        ) : null}
      </div>

      {reflection ? (
        <output className="block rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">
          {reflection}
        </output>
      ) : null}

      {/* terminal report — its own projection; a failure never dresses as success (A6-D-4) */}
      {detail.report ? <ReportSection report={detail.report} /> : null}

      {/* where it stands — progress + next step; open questions on expand (never transcripts) */}
      {!terminal &&
      (detail.progress.length > 0 || detail.next_step || detail.wait_reason) ? (
        <Card>
          <CardContent className="flex flex-col gap-2 p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {t("whereItStands")}
            </p>
            {detail.wait_reason ? (
              <p className="type-body text-amber-600 dark:text-amber-500">
                {detail.wait_reason}
              </p>
            ) : null}
            {detail.progress.length > 0 ? (
              <ul className="flex flex-col gap-1">
                {detail.progress.map((line) => (
                  <li key={line} className="type-body">
                    {line}
                  </li>
                ))}
              </ul>
            ) : null}
            {detail.next_step ? (
              <p className="type-body">
                <span className="text-muted-foreground">{t("nextStep")} </span>
                {detail.next_step}
              </p>
            ) : null}
            {detail.open_questions.length > 0 ? (
              <>
                <Button
                  variant="ghost"
                  size="sm"
                  className="-ml-2 w-fit"
                  data-icon="inline-start"
                  aria-expanded={showQuestions}
                  onClick={() => setShowQuestions((v) => !v)}
                >
                  <ChevronDown
                    className={cn(
                      "transition-transform",
                      showQuestions && "rotate-180",
                    )}
                  />
                  {t("openQuestions")}
                </Button>
                {showQuestions ? (
                  <ul className="flex flex-col gap-1">
                    {detail.open_questions.map((q) => (
                      <li key={q} className="type-body text-muted-foreground">
                        {q}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {/* budget + ledger */}
      <Card>
        <CardContent className="flex flex-col gap-2 p-4">
          <div className="flex items-center gap-2">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {t("budget")}
            </p>
            <span className="ml-auto text-sm tabular-nums">
              {t("spend", {
                spent: kr(detail.budget.spent_micros),
                cap: kr(detail.budget.cap_micros),
              })}
            </span>
          </div>
          <div className="h-1 w-full overflow-hidden rounded-full bg-muted">
            <div
              className={cn(
                "h-full rounded-full",
                detail.budget.state === "reached"
                  ? "bg-destructive"
                  : "bg-primary/50",
              )}
              style={{
                width: `${Math.min(
                  100,
                  detail.budget.cap_micros > 0
                    ? Math.round(
                        (detail.budget.spent_micros /
                          detail.budget.cap_micros) *
                          100,
                      )
                    : 0,
                )}%`,
              }}
            />
          </div>
          {detail.budget.state !== "ok" && !terminal ? (
            <div className="flex flex-wrap items-center gap-2">
              <Input
                type="number"
                inputMode="numeric"
                min={1}
                value={extendKr}
                onChange={(e) => setExtendKr(e.target.value)}
                placeholder={t("extendPlaceholder")}
                className="h-8 w-28"
                aria-label={t("extendLabel")}
              />
              <Button
                size="sm"
                variant="outline"
                disabled={busy || !extendKr}
                onClick={doExtend}
              >
                {t("extend")}
              </Button>
            </div>
          ) : null}
        </CardContent>
      </Card>

      {/* grants — what this task is allowed to do, visible */}
      {detail.grants.length > 0 ? (
        <Card>
          <CardContent className="flex flex-col gap-2 p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {t("grants")}
            </p>
            <div className="flex flex-wrap gap-1.5">
              {detail.grants.map((g) => (
                <Badge
                  key={g.category}
                  variant="outline"
                  className={cn(
                    g.decision === "gate" &&
                      "text-amber-600 dark:text-amber-500",
                    g.decision === "deny" && "text-destructive",
                  )}
                >
                  {t(`grant.${g.category}`)} · {t(`decision.${g.decision}`)}
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>
      ) : null}

      {/* checkpoint history — progress + next step (A6-D-4); NEVER raw transcripts */}
      {detail.checkpoints.length > 0 ? (
        <Card>
          <CardContent className="flex flex-col gap-3 p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {t("history")}
            </p>
            {detail.checkpoints.map((c) => (
              <div
                key={c.seq}
                className="flex flex-col gap-1 border-l-2 border-border-soft pl-3"
              >
                {c.progress_conclusions.map((line) => (
                  <p key={line} className="type-body">
                    {line}
                  </p>
                ))}
                {c.blocked_on ? (
                  <p className="type-body text-amber-600 dark:text-amber-500">
                    {c.blocked_on}
                  </p>
                ) : null}
                {c.next_step ? (
                  <p className="type-caption text-muted-foreground">
                    {t("nextStep")} {c.next_step}
                  </p>
                ) : null}
              </div>
            ))}
          </CardContent>
        </Card>
      ) : null}

      {/* controls — the B2/B4/A8 doors, in context */}
      <Card>
        <CardContent className="flex flex-col gap-3 p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("controls")}
          </p>
          {!terminal ? (
            <div className="flex flex-wrap gap-2">
              {detail.paused ? (
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() => runCommand("resume", resumeTask)}
                >
                  {t("resume")}
                </Button>
              ) : (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => runCommand("pause", pauseTask)}
                >
                  {t("pause")}
                </Button>
              )}
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => runCommand("cancel", cancelTask)}
              >
                {t("cancel")}
              </Button>
              {detail.schedule_id ? (
                <TaskReschedule
                  scheduleId={detail.schedule_id}
                  onRescheduled={refetch}
                />
              ) : null}
            </div>
          ) : null}
          <InitiativeDialControl personaId={detail.persona_id} />
          {detail.run_ids.length > 0 ? (
            <div className="flex flex-wrap gap-2">
              {detail.run_ids.map((runId) => (
                <Button
                  key={runId}
                  variant="link"
                  size="sm"
                  className="-ml-2"
                  render={<Link href={`/runs/${encodeURIComponent(runId)}`} />}
                >
                  {t("openRun")}
                </Button>
              ))}
            </div>
          ) : null}
        </CardContent>
      </Card>
    </Stack>
  );
}

function ReportSection({
  report,
}: {
  report: NonNullable<TaskDetailData["report"]>;
}) {
  const t = useTranslations("taskDetail");
  const stuck = report.kind === "stuck";
  const good = report.kind === "completed";
  return (
    <Card
      className={cn(
        "overflow-hidden border-l-2",
        stuck ? "border-l-destructive" : "border-l-border",
      )}
      data-slot="task-report"
      data-testid="task-report"
      data-kind={report.kind}
    >
      <CardContent className="flex flex-col gap-2 p-4">
        <Badge
          variant={stuck ? "destructive" : "secondary"}
          className={cn(good && "text-green-700 dark:text-green-400")}
        >
          {t(`report.${report.kind}`)}
        </Badge>
        {report.cause ? (
          <p className="type-body text-destructive">{report.cause}</p>
        ) : null}
        {(report.conclusions ?? report.where_it_stood ?? []).map((line) => (
          <p key={line} className="type-body">
            {line}
          </p>
        ))}
        {report.next_step ? (
          <p className="type-body">
            <span className="text-muted-foreground">{t("nextStep")} </span>
            {report.next_step}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
