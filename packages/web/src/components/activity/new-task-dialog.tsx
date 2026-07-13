"use client";

import { Dialog } from "@base-ui/react/dialog";
import { Play, Plus, Sparkles } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { useFormStatus } from "react-dom";

import { Button, buttonVariants } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

/**
 * Spec R11 (B2) — the kit's "Hand a task to a persona" dialog (activity.html),
 * opened from the Review header's New-task button and the Tasks tab's
 * hand-off CTA (the kit README's persistent create affordance — /tasks the
 * standalone route retired in B1).
 *
 * HONEST SUBSET (D-R11-8): the kit mocks budget-cap, autonomy and
 * schedule-for-later controls, but the only real dispatch door today is
 * `POST /v1/personas/{id}/runs` (goal + persona — the same `startTask` server
 * action the /runs page uses; budget/autonomy ride the A-track defaults).
 * Those controls arrive when an A-track create-task door exists — shipping
 * them now would render knobs that silently do nothing.
 */

export interface NewTaskPersona {
  readonly id: string;
  readonly name: string;
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
  const [goal, setGoal] = useState("");
  const [personaId, setPersonaId] = useState("");
  const ready = goal.trim().length > 0 && personaId.length > 0;
  const personaName = personas.find((p) => p.id === personaId)?.name;

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
              <label htmlFor="nt-persona" className="text-sm font-medium">
                {t("whoLabel")}
              </label>
              <select
                id="nt-persona"
                name="persona_id"
                value={personaId}
                onChange={(e) => setPersonaId(e.target.value)}
                className="h-10 rounded-md border border-border bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="" disabled>
                  {t("personaPlaceholder")}
                </option>
                {personas.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
            {ready && personaName ? (
              <div className="flex items-start gap-2.5 rounded-md border border-primary/25 bg-primary/5 px-3.5 py-3 text-sm leading-relaxed">
                <Sparkles
                  className="mt-0.5 size-4 shrink-0 text-primary"
                  aria-hidden="true"
                />
                <p className="m-0">
                  {t.rich("dialogPreview", {
                    name: personaName,
                    b: (chunks) => <b className="font-semibold">{chunks}</b>,
                  })}
                </p>
              </div>
            ) : null}
            <div className="-mx-6 mt-2 flex items-center gap-2 border-t border-border bg-muted/40 px-6 py-4">
              <Dialog.Close
                render={
                  <Button type="button" variant="ghost">
                    {t("dialogCancel")}
                  </Button>
                }
              />
              <div className="flex-1" />
              <SubmitButton disabled={!ready} />
            </div>
          </form>
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
