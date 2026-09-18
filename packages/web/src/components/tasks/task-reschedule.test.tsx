/**
 * R9-178 — <TaskReschedule> opens on the schedule's REAL cadence, never an invented one.
 *
 * The builder used to mount with no initial value, so every schedule opened as "Every day,
 * 09:00" and an untouched Apply silently rewrote an hourly schedule into a daily one. These
 * tests drive the real chain: the cadence the task detail holds seeds the picker, the chips
 * and inputs reflect it, an untouched Apply sends it back unchanged, and a cadence the picker
 * cannot represent is said out loud with the button reading "Replace".
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/i18n/messages/en.json";
import type { ScheduleCadence } from "@/lib/api/schedule-client";

import { TaskReschedule } from "./task-reschedule";

const client = vi.hoisted(() => ({
  previewReschedule: vi.fn(),
  applyReschedule: vi.fn(),
}));

vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));
vi.mock("@/components/patterns/toast", () => ({
  useToast: () => ({ error: vi.fn(), success: vi.fn() }),
}));
vi.mock("@/lib/api/schedule-client", () => client);

const HOURLY: ScheduleCadence = {
  pattern: { kind: "hourly", interval: 1, minute: 0, hour: null, weekdays: [] },
  one_time_at: null,
  timezone: "Europe/Oslo",
  human_terms: "every hour, around the clock, your time",
};

const DAILY_0700: ScheduleCadence = {
  pattern: { kind: "daily", interval: 1, hour: 7, minute: 0, weekdays: [] },
  one_time_at: null,
  timezone: "Europe/Oslo",
  human_terms: "every day at 07:00 your time",
};

const WEEKLY_MO_WE: ScheduleCadence = {
  pattern: {
    kind: "weekly",
    interval: 1,
    weekdays: ["MO", "WE"],
    hour: 18,
    minute: 30,
  },
  one_time_at: null,
  timezone: "Europe/Oslo",
  human_terms: "every Monday and Wednesday at 18:30 your time",
};

/** A rule outside the picker's vocabulary: the API served neither a pattern nor an instant. */
const UNPICKABLE: ScheduleCadence = {
  pattern: null,
  one_time_at: null,
  timezone: "Europe/Oslo",
  human_terms: "every day at 01:00, 05:00 and 09:00 your time",
};

function openDialog(current: ScheduleCadence | null) {
  render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <TaskReschedule
        scheduleId="s1"
        current={current}
        onRescheduled={vi.fn()}
      />
    </NextIntlClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: /reschedule/i }));
}

const chip = (name: string) => screen.getByRole("button", { name });
const input = (id: string) => document.querySelector(id) as HTMLInputElement;

beforeEach(() => {
  vi.clearAllMocks();
  client.applyReschedule.mockResolvedValue({
    human_terms: "",
    timezone: "Europe/Oslo",
    next_fire: null,
    quiet_hours_offer: null,
  });
});

describe("TaskReschedule opens on the current cadence", () => {
  it("an hourly schedule opens with the Hourly chip pressed and its interval", () => {
    openDialog(HOURLY);
    expect(chip("Hourly")).toHaveAttribute("aria-pressed", "true");
    expect(chip("Every day")).toHaveAttribute("aria-pressed", "false");
    expect(input("#recur-interval").value).toBe("1");
    expect(screen.getByText(/^Now: every hour/)).toBeInTheDocument();
  });

  it("a daily-at-07:00 schedule opens with Every day pressed and 07:00", () => {
    openDialog(DAILY_0700);
    expect(chip("Every day")).toHaveAttribute("aria-pressed", "true");
    expect(input("#recur-time").value).toBe("07:00");
  });

  it("a weekly schedule opens with its weekdays and time", () => {
    openDialog(WEEKLY_MO_WE);
    expect(chip("Weekly")).toHaveAttribute("aria-pressed", "true");
    expect(chip("Mon")).toHaveAttribute("aria-pressed", "true");
    expect(chip("Wed")).toHaveAttribute("aria-pressed", "true");
    expect(chip("Tue")).toHaveAttribute("aria-pressed", "false");
    expect(input("#recur-time").value).toBe("18:30");
  });

  it("an untouched Apply sends the current cadence back, in the schedule's own zone", async () => {
    openDialog(HOURLY);
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() =>
      expect(client.applyReschedule).toHaveBeenCalledTimes(1),
    );
    const body = client.applyReschedule.mock.calls[0][2];
    expect(body.pattern?.kind).toBe("hourly");
    expect(body.pattern?.interval).toBe(1);
    expect(body.timezone).toBe("Europe/Oslo");
  });

  it("a cadence the picker cannot show says so and the button reads Replace", () => {
    openDialog(UNPICKABLE);
    expect(screen.getByRole("note")).toHaveTextContent(
      /can't be shown in this picker, so applying will replace it/,
    );
    expect(screen.getByRole("note")).toHaveTextContent(
      /01:00, 05:00 and 09:00/,
    );
    expect(screen.getByRole("button", { name: "Replace" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  });

  it("no cadence at all is said out loud too, never silently defaulted", () => {
    openDialog(null);
    expect(screen.getByRole("note")).toHaveTextContent(
      /Couldn't load the current cadence/,
    );
    expect(screen.getByRole("button", { name: "Replace" })).toBeInTheDocument();
  });
});
