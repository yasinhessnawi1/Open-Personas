/**
 * Spec W1 (D-W1-4 / D-W1-28): a task-linked run takes its answer on the task page.
 *
 * The in-process respond door is retired for runs that belong to a task, so an awaiting
 * step with an `answerHref` must render a link to the task and NO inline prompt; without
 * one (a legacy bare run) the pre-W1 inline prompt is byte-for-byte what it was.
 */
import { render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import type { RunStep } from "@/lib/run";
import { StepCard } from "./step-card";

vi.mock("@/components/chat/output/dispatcher", () => ({
  OutputList: () => null,
}));
vi.mock("@/components/chat/tool-call-card", () => ({
  ToolCallCard: () => null,
}));
vi.mock("@/components/activity-state", () => ({
  ActivityState: () => null,
}));

function awaitingStep(): RunStep {
  return {
    step: 2,
    thinking: false,
    tools: [],
    outputs: [],
    question: "Which clinic?",
    answered: false,
  };
}

function mount(answerHref?: string) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <ol>
        <StepCard
          step={awaitingStep()}
          awaiting
          onAnswer={() => Promise.resolve()}
          personaId="kai"
          answerHref={answerHref}
        />
      </ol>
    </NextIntlClientProvider>,
  );
}

describe("StepCard awaiting a task-linked answer", () => {
  it("links to the task page and renders no inline prompt", () => {
    const { container } = mount("/activity/tasks/t1");
    const link = container.querySelector(
      '[data-slot="step-answer-on-task-link"]',
    );
    expect(link?.getAttribute("href")).toBe("/activity/tasks/t1");
    expect(container.textContent).toContain("Which clinic?");
    expect(container.querySelector("textarea")).toBeNull();
  });

  it("keeps the inline prompt for a legacy bare run", () => {
    const { container } = mount(undefined);
    expect(
      container.querySelector('[data-slot="step-answer-on-task-link"]'),
    ).toBeNull();
    expect(container.querySelector("textarea")).not.toBeNull();
  });
});

describe("StepCard guard notes (Spec W1, T10)", () => {
  function withNotes(notes: RunStep["notes"]) {
    return render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <ol>
          <StepCard
            step={{
              step: 1,
              thinking: false,
              tools: [],
              outputs: [],
              answered: false,
              notes,
            }}
            awaiting={false}
            onAnswer={() => Promise.resolve()}
            personaId="kai"
          />
        </ol>
      </NextIntlClientProvider>,
    );
  }

  it("names the tool whose call the run answered from its ledger", () => {
    const { container } = withNotes([
      { kind: "call_skipped", tool: "web_search" },
    ]);
    const note = container.querySelector('[data-note="call_skipped"]');

    expect(note?.textContent).toContain("web_search");
  });

  it("says when older tool output was trimmed", () => {
    const { container } = withNotes([{ kind: "context_pruned" }]);
    const note = container.querySelector('[data-note="context_pruned"]');

    expect(note?.textContent).toBe(messages.runs.noteContextPruned);
  });

  it("renders one line per guard on a step that batched several calls", () => {
    const { container } = withNotes([
      { kind: "call_skipped", tool: "web_search" },
      { kind: "call_skipped", tool: "file_read" },
      { kind: "context_pruned" },
    ]);

    expect(container.querySelectorAll('[data-slot="step-note"]')).toHaveLength(
      3,
    );
  });

  it("renders nothing at all on a step with no guard activity", () => {
    const { container } = withNotes(undefined);

    expect(container.querySelector('[data-slot="step-notes"]')).toBeNull();
  });
});
