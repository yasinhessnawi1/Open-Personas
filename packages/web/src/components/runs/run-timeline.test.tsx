/**
 * Spec W1 (T8, D-W1-34) — the timeline offers the answer on a run that is WAITING for one.
 *
 * `step-card.test.tsx` pins what an awaiting step renders. This pins which step the timeline
 * calls awaiting, which is the half that decided whether the affordance could ever appear:
 * it used to require `status === "running"`, and a leg that stops on a question ends
 * `awaiting_user`, so on a real task-linked run the link was unreachable. The T7 oracle
 * could not produce it in the browser for exactly this reason.
 */
import { render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

import messages from "@/i18n/messages/en.json";
import type { RunStatus, RunView } from "@/lib/run";

import { RunTimeline } from "./run-timeline";

vi.mock("@/components/chat/output/dispatcher", () => ({
  OutputList: () => null,
}));
vi.mock("@/components/chat/tool-call-card", () => ({
  ToolCallCard: () => null,
}));
vi.mock("@/components/activity-state", () => ({ ActivityState: () => null }));

function view(status: RunStatus): RunView {
  return {
    task: "book me a dentist appointment",
    status,
    steps: [
      { step: 1, thinking: false, tools: [], outputs: [], answered: false },
      {
        step: 2,
        thinking: false,
        tools: [],
        outputs: [],
        question: "Which dentist, and which day?",
        answered: false,
      },
    ],
  };
}

function mount(status: RunStatus) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <RunTimeline
        view={view(status)}
        onAnswer={() => Promise.resolve()}
        personaId="kai"
        answerHref="/activity/tasks/t1"
      />
    </NextIntlClientProvider>,
  );
}

const link = (c: HTMLElement) =>
  c.querySelector('[data-slot="step-answer-on-task-link"]');

describe("the answer affordance follows the question", () => {
  it("offers it on a run parked on a question (awaiting_user)", () => {
    const { container } = mount("awaiting_user");
    expect(link(container)?.getAttribute("href")).toBe("/activity/tasks/t1");
    expect(container.textContent).toContain("Which dentist, and which day?");
  });

  it("still offers it on a live run holding a question", () => {
    const { container } = mount("running");
    expect(link(container)).not.toBeNull();
  });

  it("offers nothing once the run is finished", () => {
    // A completed or cancelled run's question is history: answering it would go nowhere.
    for (const status of ["completed", "cancelled", "error"] as const) {
      const { container, unmount } = mount(status);
      expect(link(container)).toBeNull();
      unmount();
    }
  });
});
