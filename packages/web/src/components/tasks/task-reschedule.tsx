"use client";

import { CalendarClock } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useState } from "react";

import { useAuth } from "@/auth";
import { useToast } from "@/components/patterns/toast";
import {
  type CadenceInput,
  RecurrenceBuilder,
} from "@/components/schedule/recurrence-builder";
import { Button } from "@/components/ui/button";
import {
  applyReschedule,
  previewReschedule,
  type ReschedulePreview,
} from "@/lib/api/schedule-client";

/**
 * The in-context schedule edit (Spec A6, W3) for a task backed by a schedule (A6-D-1).
 *
 * Rides the SAME A8 CAS door as chat + the calendar (`applyReschedule` — no second write path). The
 * preview always shows the ENGINE's own computation of the resulting cadence; a stale edit surfaces
 * the conflict and re-previews the current state (optimistic-concurrency honest — never clobbers).
 */
export function TaskReschedule({
  scheduleId,
  onRescheduled,
}: {
  scheduleId: string;
  onRescheduled: () => void;
}) {
  const t = useTranslations("taskDetail");
  const { getToken } = useAuth();
  const toast = useToast();
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const [open, setOpen] = useState(false);
  const [cadence, setCadence] = useState<CadenceInput | null>(null);
  const [preview, setPreview] = useState<ReschedulePreview | null>(null);
  const [busy, setBusy] = useState(false);

  const doPreview = useCallback(async () => {
    if (!cadence) return;
    setBusy(true);
    try {
      setPreview(
        await previewReschedule(await getToken(), scheduleId, {
          ...cadence,
          timezone: tz,
        }),
      );
    } catch {
      toast.error(t("rescheduleFailed"));
    } finally {
      setBusy(false);
    }
  }, [cadence, getToken, scheduleId, tz, toast, t]);

  const doApply = useCallback(async () => {
    if (!cadence) return;
    setBusy(true);
    try {
      await applyReschedule(await getToken(), scheduleId, {
        ...cadence,
        timezone: tz,
      });
      onRescheduled(); // refetch the task — durable state is truth
      setOpen(false);
      setPreview(null);
    } catch {
      // a conflict/stale edit → re-preview the CURRENT state, never a silent clobber.
      toast.error(t("rescheduleConflict"));
      await doPreview();
    } finally {
      setBusy(false);
    }
  }, [cadence, getToken, scheduleId, tz, onRescheduled, toast, t, doPreview]);

  if (!open) {
    return (
      <Button
        variant="outline"
        size="sm"
        data-icon="inline-start"
        onClick={() => setOpen(true)}
      >
        <CalendarClock />
        {t("reschedule")}
      </Button>
    );
  }

  return (
    <div
      className="flex flex-col gap-3 rounded-md border border-border-soft bg-muted/30 p-3"
      data-slot="task-reschedule"
    >
      <RecurrenceBuilder timezone={tz} onChange={setCadence} />
      {preview ? (
        <div className="type-caption flex flex-col gap-0.5 text-muted-foreground">
          <span>{preview.human_terms}</span>
          {preview.quiet_hours_offer ? (
            <span className="text-amber-600 dark:text-amber-500">
              {preview.quiet_hours_offer}
            </span>
          ) : null}
        </div>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={busy || !cadence}
          onClick={doPreview}
        >
          {t("preview")}
        </Button>
        <Button size="sm" disabled={busy || !cadence} onClick={doApply}>
          {t("applyReschedule")}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
          {t("cancel")}
        </Button>
      </div>
    </div>
  );
}
