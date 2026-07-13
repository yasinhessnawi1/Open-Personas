/**
 * R9-024 — <CalendarView> persona-scoping + the delete/conflict-toast wiring.
 *
 * Verifies:
 *   1. An optional `personaId` prop threads through to `fetchOccurrences` (the
 *      chat right-panel instance's server-side filter); omitted, the call is
 *      unfiltered — matching the `/schedule` page's existing behaviour.
 *   2. The reschedule dialog's Delete button: confirms (via `useConfirm`),
 *      calls `deleteSchedule`, refreshes the sidebar, and reloads the list.
 *   3. A `schedule_state_conflict` (409) failure surfaces the NAMED conflict
 *      toast (never an uncaught rejection); a generic failure surfaces the
 *      generic one.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
// Resolves through the mock below (which spreads `...actual`) — the REAL
// ScheduleApiError class, so `instanceof` checks in calendar-view.tsx hold.
import { ScheduleApiError } from "@/lib/api/schedule-client";
import type { OccurrencesResult } from "@/lib/schedule/agenda";
import { CalendarView } from "./calendar-view";

const h = vi.hoisted(() => ({
  fetchOccurrences: vi.fn(),
  deleteSchedule: vi.fn(async () => undefined),
  applyReschedule: vi.fn(),
  previewReschedule: vi.fn(),
  confirm: vi.fn(async (_options: { title: string; tone?: string }) => true),
  notify: vi.fn(),
  refreshSidebar: vi.fn(),
  // A STABLE getToken reference (module-level, not re-created per render) —
  // CalendarView's `load` useCallback depends on it; an unstable mock
  // reference here would re-fire the load effect on every unrelated
  // re-render (an artifact of the test double, not of production code,
  // where Clerk's real useAuth() returns a stable function).
  getToken: vi.fn(async () => "test-token"),
}));

vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: h.getToken }),
}));
vi.mock("@/components/providers/confirm-provider", () => ({
  useConfirm: () => h.confirm,
}));
vi.mock("@/components/providers/notification-provider", () => ({
  useNotify: () => ({ notify: h.notify }),
}));
vi.mock("@/lib/hooks/use-sidebar-refresh", () => ({
  useSidebarRefresh: () => h.refreshSidebar,
}));
vi.mock("@/lib/api/schedule-client", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/api/schedule-client")>();
  return {
    ...actual,
    fetchOccurrences: h.fetchOccurrences,
    deleteSchedule: h.deleteSchedule,
    applyReschedule: h.applyReschedule,
    previewReschedule: h.previewReschedule,
  };
});

const messages = {
  schedule: {
    calendar: {
      deleteButton: "Delete",
      deleteConfirmTitle: "Delete this reminder?",
      deleteConfirmBody: "This can't be undone.",
      deleteFailed: "Couldn't delete this reminder. Try again.",
      conflictTitle: "Already fired",
      conflictBody: "This reminder already ran — create a new one instead.",
      rescheduleFailed: "Couldn't update this reminder. Try again.",
      previewFailed: "Couldn't preview this change. Try again.",
    },
  },
  confirm: {
    cancel: "Cancel",
    confirm: "Confirm",
    delete: "Delete",
    duplicate: "Duplicate",
    deleteTitle: "Delete {name}?",
    duplicateTitle: "Duplicate {name}?",
  },
};

function inTwoDays(): string {
  return new Date(Date.now() + 2 * 86_400_000).toISOString();
}

function resultWith(scheduleId: string): OccurrencesResult {
  return {
    occurrences: [
      {
        schedule_id: scheduleId,
        task_id: "task-1",
        persona_id: "mara",
        fire_at: inTwoDays(),
        timezone: "Europe/Oslo",
        human_terms: "every day at 09:00",
      },
    ],
    history: [],
    window_from: new Date().toISOString(),
    window_to: new Date(Date.now() + 45 * 86_400_000).toISOString(),
    truncated: false,
  };
}

function renderCalendar(personaId?: string) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <CalendarView personaId={personaId} />
    </NextIntlClientProvider>,
  );
}

describe("CalendarView — R9-024 persona scoping", () => {
  beforeEach(() => {
    h.fetchOccurrences.mockReset();
    h.deleteSchedule.mockReset().mockResolvedValue(undefined);
    h.applyReschedule.mockReset();
    h.previewReschedule.mockReset();
    h.confirm.mockReset().mockResolvedValue(true);
    h.notify.mockReset();
    h.refreshSidebar.mockReset();
    h.fetchOccurrences.mockResolvedValue(resultWith("sched-1"));
  });

  it("fetches unfiltered when personaId is omitted (the /schedule page's call)", async () => {
    renderCalendar();
    await waitFor(() => expect(h.fetchOccurrences).toHaveBeenCalledTimes(1));
    const [, , , personaIdArg] = h.fetchOccurrences.mock.calls[0];
    expect(personaIdArg).toBeUndefined();
  });

  it("threads personaId through to fetchOccurrences (the chat panel's filter)", async () => {
    renderCalendar("mara");
    await waitFor(() => expect(h.fetchOccurrences).toHaveBeenCalledTimes(1));
    const [, , , personaIdArg] = h.fetchOccurrences.mock.calls[0];
    expect(personaIdArg).toBe("mara");
  });

  it("delete: confirms (danger tone), calls deleteSchedule, refreshes sidebar + reloads", async () => {
    renderCalendar();
    await screen.findByText("every day at 09:00");
    fireEvent.click(
      screen.getByRole("button", { name: /^Reschedule: every day at 09:00/ }),
    );

    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(h.confirm).toHaveBeenCalledTimes(1));
    expect(h.confirm.mock.calls[0][0]).toMatchObject({ tone: "danger" });

    await waitFor(() =>
      expect(h.deleteSchedule).toHaveBeenCalledWith("test-token", "sched-1"),
    );
    expect(h.refreshSidebar).toHaveBeenCalledTimes(1);
    // The dialog closes + the list reloads (a 2nd fetchOccurrences call).
    await waitFor(() => expect(h.fetchOccurrences).toHaveBeenCalledTimes(2));
  });

  it("delete: a declined confirm never calls deleteSchedule", async () => {
    h.confirm.mockResolvedValue(false);
    renderCalendar();
    await screen.findByText("every day at 09:00");
    fireEvent.click(
      screen.getByRole("button", { name: /^Reschedule: every day at 09:00/ }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(h.confirm).toHaveBeenCalledTimes(1));
    expect(h.deleteSchedule).not.toHaveBeenCalled();
  });

  it("delete: a 409 schedule_state_conflict surfaces the NAMED conflict toast", async () => {
    h.deleteSchedule.mockRejectedValue(
      new ScheduleApiError(409, "/v1/me/schedule/sched-1", {
        error: "schedule_state_conflict",
        detail: "one-time schedule already fired",
      }),
    );
    renderCalendar();
    await screen.findByText("every day at 09:00");
    fireEvent.click(
      screen.getByRole("button", { name: /^Reschedule: every day at 09:00/ }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(h.notify).toHaveBeenCalledWith(
        expect.objectContaining({
          level: "error",
          title: "Already fired",
          body: "This reminder already ran — create a new one instead.",
        }),
      ),
    );
  });

  it("delete: a non-conflict failure surfaces the generic delete-failed toast", async () => {
    h.deleteSchedule.mockRejectedValue(new Error("network down"));
    renderCalendar();
    await screen.findByText("every day at 09:00");
    fireEvent.click(
      screen.getByRole("button", { name: /^Reschedule: every day at 09:00/ }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(h.notify).toHaveBeenCalledWith(
        expect.objectContaining({
          level: "error",
          title: "Couldn't delete this reminder. Try again.",
        }),
      ),
    );
  });
});
