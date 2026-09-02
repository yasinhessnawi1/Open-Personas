"use client";

import { CornerDownLeft } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { buttonVariants } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { QuestionOption } from "@/lib/sse-types";
import { cn } from "@/lib/utils";

/**
 * AskUserPrompt — the ask-user moment (spec §4.4 / spec 21 T12).
 *
 * The agentic loop (or a proactive question) blocks on a question; the answer is
 * delivered via POST /runs/:id/respond and the run resumes.
 *
 * Spec 21 (D-21-9): when `options` is present the persona offered the 3+1 shape
 * — render three option buttons plus, when `allowFreeForm` is not false, a
 * free-form field. When `options` is absent the component is byte-for-byte the
 * pre-spec-21 free-text prompt (back-compat).
 */
export function AskUserPrompt({
  question,
  options,
  allowFreeForm,
  onAnswer,
}: {
  question: string;
  options?: QuestionOption[];
  allowFreeForm?: boolean;
  onAnswer: (answer: string) => Promise<void>;
}) {
  const t = useTranslations("runs");
  const [value, setValue] = useState("");
  const [pending, setPending] = useState(false);

  const hasOptions = Array.isArray(options) && options.length > 0;
  const showFreeForm = !hasOptions || allowFreeForm !== false;

  async function submit(answer: string) {
    const trimmed = answer.trim();
    if (!trimmed || pending) return;
    setPending(true);
    try {
      await onAnswer(trimmed);
      setValue("");
    } finally {
      setPending(false);
    }
  }

  return (
    <div
      className="rounded-md border border-primary/30 bg-primary/5 p-3"
      data-slot="ask-user-prompt"
    >
      <p className="type-body mb-2 font-medium" data-slot="ask-user-question">
        {question}
      </p>

      {hasOptions ? (
        <div
          className="mb-2 flex flex-col gap-1.5"
          data-slot="ask-user-options"
        >
          {options?.map((opt) => (
            <button
              key={opt.label}
              type="button"
              disabled={pending}
              onClick={() => void submit(opt.label)}
              className={cn(
                buttonVariants({ variant: "outline", size: "sm" }),
                "h-auto justify-start gap-1 py-2 text-left",
              )}
              data-slot="ask-user-option"
            >
              <span className="font-medium">{opt.label}</span>
              {opt.description ? (
                <span className="type-caption text-muted-foreground">
                  {opt.description}
                </span>
              ) : null}
            </button>
          ))}
        </div>
      ) : null}

      {showFreeForm ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit(value);
          }}
          className="flex items-end gap-2"
        >
          <Textarea
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void submit(value);
              }
            }}
            placeholder={t("answerPlaceholder")}
            rows={1}
            disabled={pending}
            className="field-sizing-content max-h-40 min-h-10 flex-1 resize-none bg-background"
          />
          <button
            type="submit"
            disabled={pending || !value.trim()}
            className={cn(buttonVariants({ size: "sm" }), "gap-1.5")}
          >
            <CornerDownLeft className="size-3.5" aria-hidden="true" />
            {t("answer")}
          </button>
        </form>
      ) : null}
    </div>
  );
}
