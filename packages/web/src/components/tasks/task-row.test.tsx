/**
 * Spec A6 (W2) — Tasks list tests: the state matrix's honest properties.
 *
 * - **Loud-with-information (A6-D-5)** — a stuck task renders its cause + the options (Resolve /
 *   Cancel); a calm task shows no cause and no cancel.
 * - **Waiting-first ordering (A6-D-2)** — waiting_on_user → failed → active → recent-terminal.
 */
import { fireEvent, render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

import messages from "@/i18n/messages/en.json";
import type { TaskSummary } from "@/lib/api/tasks-client";

import { TaskRow } from "./task-row";
import { ordered } from "./tasks-list";

function task(overrides: Partial<TaskSummary>): TaskSummary {
  return {
    task_id: "t1",
    persona_id: "kai",
    goal: "Book the dentist",
    status: "progressing",
    paused: false,
    spent_micros: 200_000,
    budget_cap_micros: 10_000_000,
    updated_at: "2026-07-07T09:00:00Z",
    stuck_cause: null,
    ...overrides,
  };
}

function renderRow(t: TaskSummary) {
  const onCancel = vi.fn();
  const utils = render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <TaskRow task={t} personaName="Kai" busy={false} onCancel={onCancel} />
    </NextIntlClientProvider>,
  );
  return { ...utils, onCancel };
}

describe("TaskRow", () => {
  it("renders a stuck task loud — cause + options (A6-D-5)", () => {
    const { getByText, onCancel } = renderRow(
      task({ status: "failed", stuck_cause: "needs your clinic login" }),
    );
    expect(getByText("Stuck")).toBeTruthy();
    expect(getByText("needs your clinic login")).toBeTruthy(); // the cause, verbatim
    expect(getByText("Resolve")).toBeTruthy();
    fireEvent.click(getByText("Cancel"));
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("renders a calm task without a cause or cancel", () => {
    const { getByText, queryByText } = renderRow(
      task({ status: "progressing" }),
    );
    expect(getByText("In progress")).toBeTruthy();
    expect(getByText("Book the dentist")).toBeTruthy();
    expect(queryByText("Cancel")).toBeNull(); // no loud options on a healthy row
  });

  it("does not render a stuck card for waiting with no cause", () => {
    // waiting_on_user without a checkpoint reason → calm, not the loud red-rail card.
    const { queryByText, getByText } = renderRow(
      task({ status: "waiting_on_user", stuck_cause: null }),
    );
    expect(getByText("Waiting on you")).toBeTruthy();
    expect(queryByText("Resolve")).toBeNull();
  });
});

describe("ordered", () => {
  it("puts waiting-on-you first, then stuck, then active, then terminal (A6-D-2)", () => {
    const ids = ordered([
      task({ task_id: "done", status: "completed" }),
      task({ task_id: "active", status: "progressing" }),
      task({ task_id: "stuck", status: "failed", stuck_cause: "x" }),
      task({ task_id: "waiting", status: "waiting_on_user", stuck_cause: "y" }),
    ]).map((t) => t.task_id);
    expect(ids).toEqual(["waiting", "stuck", "active", "done"]);
  });
});
