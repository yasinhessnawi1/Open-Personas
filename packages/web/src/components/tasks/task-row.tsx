"use client";

import { AlertTriangle } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type { TaskSummary } from "@/lib/api/tasks-client";
import { personaIdentityStyle } from "@/lib/persona-identity";
import { cn } from "@/lib/utils";

/** The derived statuses that render loud (A6-D-5) — the one red-rail card, cause + options. */
const STUCK_STATUSES = new Set(["waiting_on_user", "failed"]);

/** Badge variant per status — semantic red (destructive) held distinct from the terracotta accent. */
function badgeVariant(status: string): "outline" | "secondary" | "destructive" {
  if (status === "failed") return "destructive";
  if (status === "waiting_on_user") return "outline";
  return "secondary";
}

/** micros → kr (1 kr = 10 000 micros, the project's credit unit); sub-10 kr keeps one decimal. */
export function kr(micros: number): string {
  const v = micros / 10_000;
  return v < 10 ? v.toFixed(1) : Math.round(v).toString();
}

interface TaskRowProps {
  task: TaskSummary;
  personaName: string;
  busy: boolean;
  onCancel: () => void;
}

export function TaskRow({ task, personaName, busy, onCancel }: TaskRowProps) {
  const t = useTranslations("taskList");
  const stuck = STUCK_STATUSES.has(task.status) && task.stuck_cause !== null;
  const cap = task.budget_cap_micros;
  const pct =
    cap > 0 ? Math.min(100, Math.round((task.spent_micros / cap) * 100)) : 0;
  const href = `/tasks/${encodeURIComponent(task.task_id)}`;

  return (
    <Card
      style={personaIdentityStyle({ id: task.persona_id })}
      className={cn(
        "overflow-hidden",
        // the ONE loud card: a red rail only when stuck (loud-only-where-it-informs).
        stuck && "border-l-2 border-l-destructive",
      )}
      data-slot="task-row"
      data-stuck={stuck}
    >
      <CardContent className="flex flex-col gap-3 p-4">
        {/* header — persona + state + last-touched (always visible) */}
        <div className="flex items-center gap-2">
          <span
            aria-hidden="true"
            className="size-2.5 rounded-[3px]"
            style={{ background: "var(--v-id)" }}
          />
          <span className="text-sm font-medium">{personaName}</span>
          <Badge
            variant={badgeVariant(task.status)}
            className={cn(
              task.status === "waiting_on_user" &&
                "text-amber-600 dark:text-amber-500",
            )}
          >
            {t(`status.${task.status}`)}
          </Badge>
          <span className="ml-auto text-xs tabular-nums text-muted-foreground">
            {t("spend", { spent: kr(task.spent_micros), cap: kr(cap) })}
          </span>
        </div>

        <Link href={href} className="type-body hover:underline">
          {task.goal}
        </Link>

        {stuck ? (
          // the cause + options (Cancel / View → detail) — loud-with-information (A6-D-5)
          <div className="flex flex-col gap-3 rounded-md border border-destructive/30 bg-destructive/5 p-3">
            <p className="flex items-start gap-1.5 text-sm text-destructive">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" />
              <span>{task.stuck_cause}</span>
            </p>
            <div className="flex flex-wrap gap-2">
              <Button render={<Link href={href} />} size="sm">
                {t("resolve")}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                disabled={busy}
                onClick={onCancel}
              >
                {t("cancel")}
              </Button>
            </div>
          </div>
        ) : (
          // a calm budget meter for the running matrix
          <div
            className="h-1 w-full overflow-hidden rounded-full bg-muted"
            aria-hidden="true"
          >
            <div
              className="h-full rounded-full bg-primary/50"
              style={{ width: `${pct}%` }}
            />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
