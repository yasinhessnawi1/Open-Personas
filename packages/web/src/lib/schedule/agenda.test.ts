/**
 * Spec A8 (T8) — the calendar view helpers render server occurrences, never recompute them.
 *
 * These pin: grouping by local day in the display tz, the HONEST truncation notice (bar 2), and
 * the fire-status labels straight from the audit-backed API (bar 6). No recurrence math is under
 * test because none exists on the client (bar 1) — the helpers only format API-provided instants.
 */

import { describe, expect, it } from "vitest";

import {
  type FireEvent,
  fireStatusLabel,
  groupByDay,
  historyByDay,
  monthGridWeeks,
  type Occurrence,
  type OccurrencesResult,
  occurrencesByDay,
  occurrenceTime,
  truncationNotice,
  weekDays,
} from "./agenda";

function occ(fireAt: string, extra: Partial<Occurrence> = {}): Occurrence {
  return {
    schedule_id: "s1",
    task_id: "t1",
    persona_id: "p1",
    fire_at: fireAt,
    timezone: "Europe/Oslo",
    human_terms: "every day at 09:00 your time",
    ...extra,
  };
}

describe("groupByDay", () => {
  it("groups occurrences by local day in the display tz, ascending", () => {
    const groups = groupByDay(
      [
        occ("2026-07-07T07:00:00Z"), // 09:00 Oslo, 7 Jul
        occ("2026-07-06T07:00:00Z"), // 09:00 Oslo, 6 Jul
        occ("2026-07-06T17:00:00Z"), // 19:00 Oslo, 6 Jul
      ],
      "Europe/Oslo",
    );
    expect(groups.map((g) => g.day)).toEqual(["2026-07-06", "2026-07-07"]);
    expect(groups[0].items).toHaveLength(2); // both 6 Jul occurrences together
    expect(groups[0].heading).toContain("6"); // "Mon 6 Jul"
  });

  it("places an instant in the correct day for the DISPLAY tz (not UTC)", () => {
    // 23:30 UTC on 6 Jul is 01:30 on 7 Jul in Oslo (CEST +2) — the display day, not the UTC day.
    const groups = groupByDay([occ("2026-07-06T23:30:00Z")], "Europe/Oslo");
    expect(groups[0].day).toBe("2026-07-07");
  });
});

describe("occurrenceTime", () => {
  it("renders the local time-of-day in the display tz", () => {
    expect(occurrenceTime(occ("2026-07-06T07:00:00Z"), "Europe/Oslo")).toBe(
      "09:00",
    );
    expect(occurrenceTime(occ("2026-07-06T07:00:00Z"), "UTC")).toBe("07:00");
  });
});

describe("truncationNotice", () => {
  const base: OccurrencesResult = {
    occurrences: [],
    history: [],
    window_from: "2026-07-01T00:00:00Z",
    window_to: "2026-09-29T00:00:00Z",
    truncated: false,
  };

  it("returns null when the full window fit (never an infinite calendar)", () => {
    expect(truncationNotice(base, "Europe/Oslo")).toBeNull();
  });

  it("names the effective horizon when truncated (bar 2)", () => {
    const notice = truncationNotice(
      { ...base, truncated: true },
      "Europe/Oslo",
    );
    expect(notice).toContain("Showing through");
    expect(notice).toContain("2026"); // the effective window_to, honestly (locale-robust)
    expect(notice).toContain("narrow the range");
  });
});

describe("fireStatusLabel", () => {
  it("maps audit-backed statuses honestly — no synthesis (bar 6)", () => {
    expect(fireStatusLabel("ran")).toBe("Ran");
    expect(fireStatusLabel("ran_late")).toBe("Ran (late)");
    expect(fireStatusLabel("missed")).toBe("Missed");
  });
});

describe("monthGridWeeks", () => {
  it("is a 6×7 Monday-start grid whose in-month cells span the month", () => {
    // July 2026: 1 Jul is a Wednesday → the first row starts Mon 29 Jun.
    const weeks = monthGridWeeks(2026, 6);
    expect(weeks).toHaveLength(6);
    expect(weeks.every((w) => w.length === 7)).toBe(true);
    expect(weeks[0][0].day).toBe("2026-06-29"); // Monday of the first week (leading, out of month)
    expect(weeks[0][0].inMonth).toBe(false);
    expect(weeks[0][2].day).toBe("2026-07-01"); // Wed 1 Jul, in month
    expect(weeks[0][2].inMonth).toBe(true);
    const inMonth = weeks.flat().filter((c) => c.inMonth);
    expect(inMonth).toHaveLength(31); // every July day, exactly once
    expect(inMonth[30].day).toBe("2026-07-31");
  });
});

describe("weekDays", () => {
  it("returns the 7 Monday-start day keys of the anchor's week", () => {
    const days = weekDays(new Date("2026-07-08T12:00:00Z")); // a Wednesday
    expect(days).toHaveLength(7);
    expect(days[0]).toBe("2026-07-06"); // Monday
    expect(days[6]).toBe("2026-07-12"); // Sunday
  });
});

describe("occurrencesByDay / historyByDay", () => {
  it("keys occurrences + history by the display-tz calendar day (grid cell lookup)", () => {
    const byDay = occurrencesByDay(
      [occ("2026-07-06T23:30:00Z")],
      "Europe/Oslo",
    );
    expect(byDay.get("2026-07-07")).toHaveLength(1); // 01:30 Oslo → 7 Jul cell

    const hist: FireEvent[] = [
      { schedule_id: "s1", at: "2026-07-06T07:00:00Z", status: "ran" },
    ];
    expect(historyByDay(hist, "Europe/Oslo").get("2026-07-06")).toEqual([
      "ran",
    ]);
  });
});
