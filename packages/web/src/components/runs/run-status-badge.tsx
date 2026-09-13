import { useTranslations } from "next-intl";
import type { RunStatus } from "@/lib/run";
import { cn } from "@/lib/utils";

/**
 * Spec F2 T30 — RunStatusBadge (retokenised).
 *
 * Status reads as a temperature: live=vermilion pulse, done=cool, faults=warm.
 * The semantic palette stays; T30 closes the `text-[0.65rem] tracking-wide`
 * legacy via F1's `.type-caption` token (resolves through `--text-caption-*`).
 */

const STATUS_CLASS: Record<RunStatus, string> = {
  running: "border-primary/40 text-primary",
  // Spec W1: waiting on a person, the same amber the review page uses for "needs you".
  awaiting_user: "border-amber-500/45 text-amber-600 dark:text-amber-500",
  completed: "border-tier-small/50 text-tier-small",
  cancelled: "border-border text-muted-foreground",
  max_steps_reached: "border-tier-mid/50 text-tier-mid",
  error: "border-destructive/50 text-destructive",
};

export function RunStatusBadge({ status }: { status: RunStatus }) {
  const t = useTranslations("runs");
  return (
    <span
      title={t("statusLabel")}
      data-slot="run-status-badge"
      data-status={status}
      className={cn(
        "type-caption inline-flex w-fit items-center gap-1.5 rounded border px-2 py-0.5 font-mono uppercase",
        STATUS_CLASS[status],
      )}
    >
      {status === "running" ? (
        <span
          aria-hidden="true"
          className="size-1.5 animate-pulse rounded-full bg-primary"
        />
      ) : null}
      {t(`status.${status}`)}
    </span>
  );
}
