"use client";

import { Dialog } from "@base-ui/react/dialog";
import { CalendarClock, Play, Plus, Sparkles } from "lucide-react";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { useState } from "react";
import { useFormStatus } from "react-dom";

import { useAuth } from "@/auth";
import { ExecutorPicker } from "@/components/persona/executor-picker";
import { Button, buttonVariants } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { createSchedule } from "@/lib/api/schedule-client";
import { cn } from "@/lib/utils";

/**
 * Spec R11 (B2) — the kit's "Hand a task to a persona" dialog (activity.html),
 * opened from the Review header's New-task button and the Tasks tab's
 * hand-off CTA (the kit README's persistent create affordance — /tasks the
 * standalone route retired in B1).
 *
 * Two real doors, per the kit's footer:
 *   - **Start now** — `POST /v1/personas/{id}/runs` (the /runs `startTask`
 *     server action, threaded as a prop — the NewTaskForm pattern).
 *   - **Schedule for later** — the A10 one-door (`POST /v1/me/schedule`) with
 *     `intent: "task"`, so the backing task carries the goal VERBATIM and
 *     shows up under Tasks as Scheduled until it fires (A4 Option-B: all
 *     tasks are schedule-backed).
 *
 * HONEST SUBSET (D-R11-8): the kit's budget-cap / autonomy knobs are NOT
 * shipped — no dispatch door carries them yet; knobs that silently do nothing
 * would violate the honesty rule.
 */

export interface NewTaskPersona {
  readonly id: string;
  readonly name: string;
  /** Real avatar for the shared executor picker (R11-B3). */
  readonly avatar_url?: string | null;
}

/** The dispatch server action (the /runs `startTask` door), passed down from a
 * server page — same pattern as `NewTaskForm` — so this client component never
 * imports server-only modules. */
export type NewTaskAction = (formData: FormData) => void | Promise<void>;

function SubmitButton({ disabled }: { disabled: boolean }) {
  const { pending } = useFormStatus();
  const t = useTranslations("tasks");
  return (
    <Button type="submit" disabled={pending || disabled} className="gap-2">
      <Play className="size-4" aria-hidden="true" />
      {pending ? t("dispatching") : t("startNow")}
    </Button>
  );
}

/** The dialog body — mounted fresh per open (state resets; the idempotency key
 * is minted once per dialog-open, A10-D-6). */
function DialogBody({
  personas,
  action,
}: {
  personas: readonly NewTaskPersona[];
  action: NewTaskAction;
}) {
  const t = useTranslations("tasks");
  const locale = useLocale();
  const router = useRouter();
  const { getToken } = useAuth();

  const [goal, setGoal] = useState("");
  const [personaId, setPersonaId] = useState("");
  const [when, setWhen] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [idempotencyKey] = useState(() => crypto.randomUUID());

  const ready = goal.trim().length > 0 && personaId.length > 0;
  const personaName = personas.find((p) => p.id === personaId)?.name;
  const whenDate = when ? new Date(when) : null;
  const whenValid =
    whenDate !== null &&
    !Number.isNaN(whenDate.getTime()) &&
    whenDate.getTime() > Date.now();
  const scheduling = when.length > 0;

  async function schedule() {
    if (!ready || !whenValid || whenDate === null) return;
    setBusy(true);
    setError(null);
    try {
      const result = await createSchedule(await getToken(), {
        pattern: null,
        one_time_at: whenDate.toISOString(),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        persona_id: personaId,
        subject: goal.trim(),
        idempotency_key: idempotencyKey,
        notify_on_fire: true,
        intent: "task",
      });
      router.push(`/activity/tasks/${encodeURIComponent(result.task_id)}`);
    } catch {
      setError(t("scheduleError"));
      setBusy(false);
    }
  }

  return (
    <form action={action} className="flex flex-col gap-4 px-6 pt-4">
      <div className="flex flex-col gap-1.5">
        <label htmlFor="nt-goal" className="text-sm font-medium">
          {t("goalLabel")}
        </label>
        <Textarea
          id="nt-goal"
          name="task"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder={t("taskPlaceholder")}
          className="min-h-16 field-sizing-content resize-y"
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <span className="text-sm font-medium">{t("whoLabel")}</span>
        {/* R11-B3 (owner-ruled): the SHARED persona picker; the hidden input
            keeps the server-action form contract (name="persona_id"). */}
        <ExecutorPicker
          personas={personas}
          value={personaId}
          onSelect={setPersonaId}
          label={t("whoLabel")}
          placeholder={t("personaPlaceholder")}
        />
        <input type="hidden" name="persona_id" value={personaId} />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="nt-when" className="text-sm font-medium">
          {t("whenLabel")}
        </label>
        <input
          id="nt-when"
          type="datetime-local"
          value={when}
          onChange={(e) => setWhen(e.target.value)}
          className="h-10 rounded-md border border-border bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <p className="text-xs text-muted-foreground">
          {scheduling && !whenValid ? (
            <span className="text-destructive">{t("schedulePast")}</span>
          ) : (
            t("whenHint")
          )}
        </p>
      </div>
      {ready && personaName ? (
        <div className="flex items-start gap-2.5 rounded-md border border-primary/25 bg-primary/5 px-3.5 py-3 text-sm leading-relaxed">
          <Sparkles
            className="mt-0.5 size-4 shrink-0 text-primary"
            aria-hidden="true"
          />
          <p className="m-0">
            {scheduling && whenValid && whenDate
              ? t.rich("schedulePreview", {
                  name: personaName,
                  when: whenDate.toLocaleString(locale, {
                    weekday: "short",
                    day: "numeric",
                    month: "short",
                    hour: "2-digit",
                    minute: "2-digit",
                  }),
                  b: (chunks) => <b className="font-semibold">{chunks}</b>,
                })
              : t.rich("dialogPreview", {
                  name: personaName,
                  b: (chunks) => <b className="font-semibold">{chunks}</b>,
                })}
          </p>
        </div>
      ) : null}
      {error ? <p className="text-sm text-destructive">{error}</p> : null}
      <div className="-mx-6 mt-2 flex items-center gap-2 border-t border-border bg-muted/40 px-6 py-4">
        <Dialog.Close
          render={
            <Button type="button" variant="ghost">
              {t("dialogCancel")}
            </Button>
          }
        />
        <div className="flex-1" />
        {scheduling ? (
          <Button
            type="button"
            disabled={!ready || !whenValid || busy}
            onClick={() => void schedule()}
            className="gap-2"
          >
            <CalendarClock className="size-4" aria-hidden="true" />
            {busy ? t("scheduling") : t("scheduleForLater")}
          </Button>
        ) : (
          <SubmitButton disabled={!ready} />
        )}
      </div>
    </form>
  );
}

export function NewTaskDialog({
  personas,
  action,
  trigger,
}: {
  personas: readonly NewTaskPersona[];
  action: NewTaskAction;
  /** Optional custom trigger element; defaults to the primary "New task" button. */
  trigger?: React.ReactElement;
}) {
  const t = useTranslations("tasks");

  if (personas.length === 0) return null;

  return (
    <Dialog.Root>
      <Dialog.Trigger
        render={
          trigger ?? (
            <button type="button" className={cn(buttonVariants(), "gap-2")}>
              <Plus className="size-4" aria-hidden="true" />
              {t("newHeading")}
            </button>
          )
        }
      />
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-50 bg-black/30 transition-opacity duration-[var(--motion-duration-fast)] data-ending-style:opacity-0 data-starting-style:opacity-0 supports-backdrop-filter:backdrop-blur-xs" />
        <Dialog.Popup className="fixed left-1/2 top-[12vh] z-50 w-[min(520px,calc(100vw-2rem))] -translate-x-1/2 overflow-hidden rounded-2xl border border-border bg-card shadow-xl outline-none">
          <div className="px-6 pt-6">
            <Dialog.Title className="font-heading text-xl font-semibold">
              {t("dialogTitle")}
            </Dialog.Title>
            <Dialog.Description className="mt-1.5 text-sm leading-relaxed text-muted-foreground">
              {t("dialogIntro")}
            </Dialog.Description>
          </div>
          <DialogBody personas={personas} action={action} />
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/**
 * The Tasks tab's hand-off CTA (kit `tasks-cta`): a dashed invitation box with
 * the same dialog behind it.
 */
export function NewTaskCta({
  personas,
  action,
}: {
  personas: readonly NewTaskPersona[];
  action: NewTaskAction;
}) {
  const t = useTranslations("tasks");
  if (personas.length === 0) return null;
  return (
    <div
      className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border-[1.5px] border-dashed border-border px-4 py-4"
      data-slot="new-task-cta"
    >
      <div>
        <p className="text-sm font-medium">{t("ctaTitle")}</p>
        <p className="mt-0.5 text-xs text-muted-foreground">{t("ctaBody")}</p>
      </div>
      <NewTaskDialog personas={personas} action={action} />
    </div>
  );
}
