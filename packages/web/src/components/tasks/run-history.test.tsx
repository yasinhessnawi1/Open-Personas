/**
 * R9-158: <RunHistory> says why a run stopped, and says the next run is starting.
 *
 * - A run stopped early shows its reason in the owner's words, not a bare "cancelled".
 *   The strings are asserted literally, so a copy change is a deliberate test change.
 * - While a leg is queued and no run is live (right after Resume), a "Starting" row leads
 *   the list; it goes as soon as the run is live, and never shows when nothing is queued.
 */
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";

import messages from "@/i18n/messages/en.json";
import type { TaskRun } from "@/lib/api/tasks-client";

import { RunHistory } from "./run-history";

function run(overrides: Partial<TaskRun>): TaskRun {
  return {
    id: "run-1",
    persona_id: "kai",
    task: "Book the dentist",
    task_id: "t1",
    status: "cancelled",
    stop_reason: null,
    started_at: "2026-09-26T09:00:00Z",
    finished_at: "2026-09-26T09:05:00Z",
    ...overrides,
  };
}

function renderHistory(runs: TaskRun[], legQueued?: boolean) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <RunHistory runs={runs} legQueued={legQueued} />
    </NextIntlClientProvider>,
  );
}

function slotTexts(slot: string): string[] {
  return Array.from(document.querySelectorAll(`[data-slot="${slot}"]`)).map(
    (el) => el.textContent ?? "",
  );
}

const STARTING =
  "Starting. The next run appears here as soon as a worker picks it up.";

describe("RunHistory: why a run stopped (R9-158)", () => {
  it.each([
    ["paused", "Stopped: you paused this task"],
    ["cancelled", "Stopped: you cancelled this task"],
    ["budget", "Stopped: budget reached"],
    ["wall_clock", "Stopped: time limit reached"],
    ["steps", "Stopped: step limit reached"],
    ["drain", "Stopped for a restart"],
    ["approval", "Stopped: it was waiting for your approval"],
  ])("a run stopped for %s says so in plain words", (reason, text) => {
    renderHistory([run({ stop_reason: reason })]);
    expect(slotTexts("run-history-stop-reason")).toEqual([text]);
  });

  it("a run that ended on its own, or before reasons were recorded, shows no reason", () => {
    renderHistory([
      run({ id: "a", status: "completed", stop_reason: null }),
      run({ id: "b", status: "cancelled" }),
    ]);
    expect(slotTexts("run-history-stop-reason")).toEqual([]);
  });

  it("an unknown reason shows no line rather than a raw key", () => {
    renderHistory([run({ stop_reason: "restart" })]);
    expect(slotTexts("run-history-stop-reason")).toEqual([]);
  });

  it("the stop reasons carry no em or en dashes", () => {
    const reasons = Object.values(messages.taskDetail.stopReason);
    const dashes = [0x2013, 0x2014].map((code) => String.fromCharCode(code));
    expect(
      reasons.filter((text) => dashes.some((dash) => text.includes(dash))),
    ).toEqual([]);
  });
});

describe("RunHistory: the next run is starting (R9-158)", () => {
  it("leads with a Starting row when a leg is queued and nothing runs yet", () => {
    renderHistory([run({ stop_reason: "paused" })], true);
    expect(slotTexts("run-history-starting")).toEqual([STARTING]);
    // The old stopped run is still listed, below the Starting row.
    const rows = Array.from(
      document.querySelectorAll('[data-slot="run-history"] > li'),
    ).map((el) => el.getAttribute("data-slot"));
    expect(rows).toEqual(["run-history-starting", "run-history-row"]);
  });

  it("announces Starting to assistive technology as a status", () => {
    renderHistory([], true);
    expect(screen.getByRole("status").textContent).toBe(STARTING);
  });

  it("shows Starting instead of the empty line before the first run", () => {
    renderHistory([], true);
    expect(slotTexts("run-history-starting")).toEqual([STARTING]);
    expect(slotTexts("run-history-empty")).toEqual([]);
  });

  it("drops the Starting row once the run is live", () => {
    renderHistory([run({ status: "running", finished_at: null })], true);
    expect(slotTexts("run-history-starting")).toEqual([]);
  });

  it("never shows Starting when no leg is queued", () => {
    renderHistory([run({ stop_reason: "paused" })], false);
    expect(slotTexts("run-history-starting")).toEqual([]);
    renderHistory([]);
    expect(screen.getByText(messages.taskDetail.runsEmpty)).toBeInTheDocument();
  });
});
