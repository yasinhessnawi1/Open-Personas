/**
 * Spec A6 (W3) — <TaskDetail> tests: the honest-reflection + honest-projection properties.
 *
 * - **Calm reflection (condition 1)** — a resume while owner-autonomy is paused shows the pause,
 *   never implies it armed; the durable post-state is refetched.
 * - **Terminal projection (A6-D-4, condition 4)** — a stuck report renders as its own stuck
 *   projection (a failure never dresses as success).
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
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
    kind: "standing",
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
    runs: [],
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

  it("lists the files handed over with the task on a fresh fetch (issue #16)", async () => {
    // Reopened parity: the attachments are read off the contract the GET returns, so the
    // page shows what the persona actually works from, not what a dialog once held.
    client.getTask.mockResolvedValue(
      detail({
        attachments: [
          {
            ref: "uploads/9f2c0a.pdf",
            filename: "quarter.pdf",
            media_type: "application/pdf",
          },
          {
            ref: "uploads/1a0b.csv",
            filename: "rows.csv",
            media_type: "text/csv",
          },
        ],
      }),
    );
    renderDetail();

    expect(await screen.findByText("Files handed over")).toBeInTheDocument();
    expect(screen.getByText("quarter.pdf")).toBeInTheDocument();
    expect(screen.getByText("rows.csv")).toBeInTheDocument();
  });

  it("says nothing about files when none were handed over", async () => {
    client.getTask.mockResolvedValue(detail({}));
    renderDetail();
    await screen.findByText("Book the dentist");
    expect(screen.queryByText("Files handed over")).toBeNull();
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

describe("run history (Spec W1, D-W1-3)", () => {
  it("lists the task's runs with a link into each viewer, newest first", async () => {
    client.getTask.mockResolvedValue(
      detail({
        kind: "ad_hoc",
        runs: [
          {
            id: "run_2",
            persona_id: "kai",
            task: "Book the dentist",
            task_id: "t1",
            status: "running",
            started_at: "2026-09-06T12:00:00Z",
            finished_at: null,
          },
          {
            id: "run_1",
            persona_id: "kai",
            task: "Book the dentist",
            task_id: "t1",
            status: "completed",
            started_at: "2026-09-06T11:00:00Z",
            finished_at: "2026-09-06T11:01:00Z",
          },
        ],
      }),
    );
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <TaskDetail taskId="t1" personaNames={{ kai: "Kai" }} />
      </NextIntlClientProvider>,
    );
    const history = await waitFor(() => {
      const el = document.querySelector('[data-slot="run-history"]');
      if (!el) throw new Error("no run history yet");
      return el;
    });
    const items = history.querySelectorAll('[data-slot="run-history-row"]');
    expect(items).toHaveLength(2);
    expect(items[0].getAttribute("data-status")).toBe("running");
    expect(items[0].querySelector("a")?.getAttribute("href")).toBe(
      "/runs/run_2",
    );
    expect(items[1].querySelector("a")?.getAttribute("href")).toBe(
      "/runs/run_1",
    );
  });

  it("says plainly when no run has opened yet", async () => {
    client.getTask.mockResolvedValue(detail({ runs: [] }));
    render(
      <NextIntlClientProvider locale="en" messages={messages}>
        <TaskDetail taskId="t1" personaNames={{ kai: "Kai" }} />
      </NextIntlClientProvider>,
    );
    await waitFor(() => {
      expect(
        document.querySelector('[data-slot="run-history-empty"]'),
      ).not.toBeNull();
    });
  });

  // --- R9-158: Resume's first refresh, and the poll that follows a queued leg -----------

  const stoppedByPause: TaskDetailData["runs"][number] = {
    id: "run_1",
    persona_id: "kai",
    task: "Book the dentist",
    task_id: "t1",
    status: "cancelled",
    stop_reason: "paused",
    started_at: "2026-09-26T09:00:00Z",
    finished_at: "2026-09-26T09:02:00Z",
  };

  it("shows the next run starting after Resume, never the stale paused pair", async () => {
    client.getTask
      .mockResolvedValueOnce(
        detail({ status: "paused", paused: true, runs: [stoppedByPause] }),
      )
      .mockResolvedValue(
        detail({
          status: "progressing",
          paused: false,
          leg_queued: true,
          runs: [stoppedByPause],
        }),
      );
    client.resumeTask.mockResolvedValue({
      task_id: "t1",
      status: "progressing",
      paused: false,
      changed: true,
      owner_autonomy_paused: false,
      note: "",
    });
    renderDetail();
    fireEvent.click(await screen.findByRole("button", { name: "Resume" }));

    await screen.findByText(
      "Starting. The next run appears here as soon as a worker picks it up.",
    );
    const slots = Array.from(
      document.querySelectorAll('[data-slot="run-history"] > li'),
    ).map((el) => el.getAttribute("data-slot"));
    expect(slots).toEqual(["run-history-starting", "run-history-row"]);
    expect(
      document.querySelector('[data-slot="run-history-stop-reason"]')
        ?.textContent,
    ).toBe("Stopped: you paused this task");
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
  });

  it.each([
    [true, 2],
    [false, 1],
  ])(
    "a queued leg (%s) keeps the page polling even while the task waits on the user",
    async (legQueued, fetches) => {
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
      try {
        client.getTask.mockResolvedValue(
          detail({ status: "waiting_on_user", leg_queued: legQueued }),
        );
        renderDetail();
        await screen.findByText("Book the dentist");
        await act(async () => {
          vi.advanceTimersByTime(3000);
        });
        expect(client.getTask).toHaveBeenCalledTimes(fetches);
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it.each([
    ["a finished task", { status: "completed" }],
    ["a paused task", { status: "paused", paused: true }],
  ])(
    "%s shows no Starting row and does not poll, even if a leg is still listed",
    async (_label, state) => {
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
      try {
        client.getTask.mockResolvedValue(
          detail({ ...state, leg_queued: true, runs: [stoppedByPause] }),
        );
        renderDetail();
        await screen.findByText("Book the dentist");
        await act(async () => {
          vi.advanceTimersByTime(9000);
        });
        expect(
          document.querySelector('[data-slot="run-history-starting"]'),
        ).toBeNull();
        expect(client.getTask).toHaveBeenCalledTimes(1);
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it("a finished task stops polling even if a run row still says running", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      client.getTask.mockResolvedValue(
        detail({
          status: "completed",
          runs: [{ ...stoppedByPause, status: "running", stop_reason: null }],
        }),
      );
      renderDetail();
      await screen.findByText("Book the dentist");
      await act(async () => {
        vi.advanceTimersByTime(9000);
      });
      expect(client.getTask).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
