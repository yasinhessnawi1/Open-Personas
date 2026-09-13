"use client";

import { useTranslations } from "next-intl";
import { personaIdentityStyle } from "@/lib/persona-identity";
import type { RunView } from "@/lib/run";
import { StepCard } from "./step-card";

/**
 * Spec F2 T30 — RunTimeline (retokenised).
 *
 * Vertical timeline of `<StepCard>`s with a left-rail connector + a running
 * tail indicator while the agent is working between steps. Behaviour
 * preserved verbatim (per audit.md §runs.plumbing); presentation closed:
 * `text-sm` → `.type-ui` for the "working" / "no steps" labels.
 */
export function RunTimeline({
  view,
  onAnswer,
  personaId,
  answerHref,
}: {
  view: RunView;
  onAnswer: (answer: string) => Promise<void>;
  /** F4 T11: drilled down from RunView → StepCard for the byte-load auth. */
  personaId: string;
  /** Spec W1: where a task-linked run takes its answer (the task page), if anywhere. */
  answerHref?: string;
}) {
  const t = useTranslations("runs");
  const running = view.status === "running";
  // Spec W1 (D-W1-34): a leg that stopped ON a question ends `awaiting_user`, not `running`.
  // The unanswered question is exactly what the reader can act on, so the affordance follows
  // the QUESTION in both states; the working tail below still follows `running` alone.
  const answerable = running || view.status === "awaiting_user";
  const awaitingStep = answerable
    ? view.steps.find((s) => s.question && !s.answered)?.step
    : undefined;

  if (view.steps.length === 0 && !running) {
    return (
      <p
        className="type-ui text-muted-foreground"
        data-slot="run-timeline-empty"
      >
        {t("noSteps")}
      </p>
    );
  }

  const tailWorking = running && awaitingStep === undefined;

  return (
    <div
      className="relative"
      style={personaIdentityStyle({ id: personaId })}
      data-slot="run-timeline"
    >
      {/* Each step carries its own .v-run-rail (dot + connector); the list has
          no gap so the connectors form one continuous spine. */}
      <ol className="flex flex-col">
        {view.steps.map((s, i) => (
          <StepCard
            key={s.step}
            step={s}
            awaiting={s.step === awaitingStep}
            last={i === view.steps.length - 1 && !tailWorking}
            onAnswer={onAnswer}
            personaId={personaId}
            answerHref={answerHref}
          />
        ))}
      </ol>
      {tailWorking ? (
        <div
          className="v-run-step"
          data-slot="run-timeline-working"
          aria-live="polite"
        >
          <div className="v-run-rail">
            <span
              className="v-run-dot"
              data-state="running"
              aria-hidden="true"
            />
          </div>
          <div className="type-ui flex items-center pt-0.5 text-muted-foreground">
            {t("working")}
          </div>
        </div>
      ) : null}
    </div>
  );
}
