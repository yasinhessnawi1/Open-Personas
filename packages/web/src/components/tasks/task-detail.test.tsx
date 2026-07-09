/**
 * Spec A6 (W3) — <TaskDetail> tests: the honest-reflection + honest-projection properties.
 *
 * - **Calm reflection (condition 1)** — a resume while owner-autonomy is paused shows the pause,
 *   never implies it armed; the durable post-state is refetched.
 * - **Terminal projection (A6-D-4, condition 4)** — a stuck report renders as its own stuck
 *   projection (a failure never dresses as success).
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/i18n/messages/en.json";
import type { TaskDetail as TaskDetailData } from "@/lib/api/tasks-client";

import { TaskDetail } from "./task-detail";

const client = vi.hoisted(() => ({
  getTask: vi.fn(),
  pauseTask: vi.fn(),
  resumeTask: vi.fn(),
  cancelTask: vi.fn(),
  extendBudget: vi.fn(),
  getInitiativeDial: vi.fn(),
  setInitiativeDial: vi.fn(),
}));

// A STABLE getToken (like the real useAuth) — a fresh identity each render would re-fire the
// load effect and inflate the refetch count.
const auth = vi.hoisted(() => ({
  getToken: () => Promise.resolve("test-token"),
}));

// R9-012: a cancel refreshes the sidebar via useSidebarRefresh.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn() }),
}));

vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: auth.getToken }),
}));
vi.mock("@/components/patterns/toast", () => ({
  useToast: () => ({ error: vi.fn(), success: vi.fn() }),
}));
vi.mock("@/lib/api/tasks-client", () => client);

function detail(overrides: Partial<TaskDetailData>): TaskDetailData {
  return {
    task_id: "t1",
    persona_id: "kai",
    goal: "Book the dentist",
    scope: "",
    status: "progressing",
    paused: false,
    grants: [{ category: "observe", decision: "allow" }],
    acceptance_criteria: [],
    deadline: null,
    max_legs: null,
    budget: { cap_micros: 10_000_000, spent_micros: 200_000, state: "ok" },
    ledger: {
      model_micros: 200_000,
      sandbox_micros: 0,
      external_micros: 0,
      total_micros: 200_000,
    },
    progress: [],
    next_step: "",
    open_questions: [],
    wait_reason: null,
    report: null,
    checkpoints: [],
    conversation_id: null,
    schedule_id: null,
    run_ids: [],
    created_at: "2026-07-07T09:00:00Z",
    updated_at: "2026-07-07T09:00:00Z",
    ...overrides,
  };
}

function renderDetail() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <TaskDetail taskId="t1" personaNames={{ kai: "Kai" }} />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  client.getInitiativeDial.mockResolvedValue({
    persona_id: "kai",
    dial: "propose_only",
    changed: false,
    initiative_enabled: false,
    note: "",
  });
});

describe("TaskDetail", () => {
  it("reflects a resume-while-owner-paused honestly (never implies it armed)", async () => {
    client.getTask.mockResolvedValue(detail({ paused: true }));
    client.resumeTask.mockResolvedValue({
      task_id: "t1",
      status: "just_created",
      paused: false,
      changed: true,
      owner_autonomy_paused: true,
      note: "Resumed — but your autonomy is paused, so it won't run until you resume autonomy.",
    });
    renderDetail();

    const resume = await screen.findByRole("button", { name: "Resume" });
    fireEvent.click(resume);

    // the durable note is reflected verbatim — the pause is shown, not silently armed.
    expect(
      await screen.findByText(/your autonomy is paused/i),
    ).toBeInTheDocument();
    expect(client.resumeTask).toHaveBeenCalledOnce();
    expect(client.getTask).toHaveBeenCalledTimes(2); // initial + durable refetch
  });

  it("renders a stuck terminal report as its own projection (not success)", async () => {
    client.getTask.mockResolvedValue(
      detail({
        status: "failed",
        report: {
          kind: "stuck",
          cause: "the clinic portal needs your login",
          where_it_stood: ["found the booking page"],
          next_step: "share the login and I'll finish",
        },
      }),
    );
    renderDetail();

    const region = await screen.findByTestId("task-report");
    expect(region.getAttribute("data-kind")).toBe("stuck");
    expect(
      screen.getByText("the clinic portal needs your login"),
    ).toBeInTheDocument();
    // no pause/resume/cancel on a terminal task — the controls are not offered.
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    });
  });
});
