/**
 * Spec A8 (T8) — the calendar's pure view helpers.
 *
 * These render occurrences the SERVER computed (from the one engine, A8-D-11); they do ZERO
 * recurrence math — no RRULE expansion, no next-fire computation. The client only groups and
 * formats absolute instants the API returned (`Intl.DateTimeFormat` in the display timezone).
 * Keeping this the only schedule logic on the client is the thin-frontend line (criterion 5):
 * grep the web tree for `rrule`/`recurrence` math and you find none.
 */

export type FireStatus = "ran" | "ran_late" | "missed";

export interface Occurrence {
  schedule_id: string;
  task_id: string | null;
  persona_id: string | null;
  /** The absolute UTC instant the schedule fires (ISO-8601). Formatted for display only. */
  fire_at: string;
  /** The schedule's captured IANA zone (for the human-terms clause). */
  timezone: string;
  /** The cadence in human terms — never a raw RRULE. */
  human_terms: string;
  /** R11-B3: WHAT fires (the A10 subject, else the backing task's goal) — the
   * calendar's display line; null falls back to human_terms. */
  subject?: string | null;
}

export interface FireEvent {
  schedule_id: string;
  at: string;
  status: FireStatus;
}

export interface OccurrencesResult {
  occurrences: Occurrence[];
  history: FireEvent[];
  window_from: string;
  /** The EFFECTIVE end after the server's horizon clamp. */
  window_to: string;
  /** True iff the server capped the window/count — render honestly, never an infinite calendar. */
  truncated: boolean;
}

export interface DayGroup {
  /** The local calendar day key (YYYY-MM-DD in the display tz). */
  day: string;
  /** A human day heading ("Mon 6 Jul"). */
  heading: string;
  items: Occurrence[];
}

export function dayKey(iso: string, tz: string): string {
  // en-CA yields YYYY-MM-DD; the tz option places the instant in the display day.
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(iso));
}

/** Format an absolute instant in the display timezone (display only — no recurrence math). */
export function formatInTz(
  iso: string,
  tz: string,
  opts: Intl.DateTimeFormatOptions,
): string {
  return new Intl.DateTimeFormat(undefined, { timeZone: tz, ...opts }).format(
    new Date(iso),
  );
}

/** The local time-of-day of an occurrence in the display tz (e.g. "09:00"). */
export function occurrenceTime(occ: Occurrence, tz: string): string {
  return formatInTz(occ.fire_at, tz, {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** Group occurrences by local day in the display tz, ascending — the agenda view's shape. */
export function groupByDay(occurrences: Occurrence[], tz: string): DayGroup[] {
  const byDay = new Map<string, Occurrence[]>();
  const sorted = [...occurrences].sort((a, b) =>
    a.fire_at.localeCompare(b.fire_at),
  );
  for (const occ of sorted) {
    const key = dayKey(occ.fire_at, tz);
    const bucket = byDay.get(key);
    if (bucket) bucket.push(occ);
    else byDay.set(key, [occ]);
  }
  return [...byDay.entries()].map(([day, items]) => ({
    day,
    heading: formatInTz(items[0].fire_at, tz, {
      weekday: "short",
      day: "numeric",
      month: "short",
    }),
    items,
  }));
}

/**
 * The `schedule.calendar` message getter (next-intl's `t`), passed in so these
 * stay pure functions that never reach for a React hook.
 */
export type ScheduleTranslator = (
  key: string,
  values?: Record<string, string | number>,
) => string;

/**
 * The honest truncation notice, or null when the full window fit (never an infinite-looking
 * calendar). "Showing through <date>" names the effective horizon the server clamped to.
 */
export function truncationNotice(
  result: OccurrencesResult,
  tz: string,
  t: ScheduleTranslator,
): string | null {
  if (!result.truncated) return null;
  const through = formatInTz(result.window_to, tz, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
  return t("truncationNotice", { date: through });
}

/** The honest fire-history label straight from the audit-backed API status (no synthesis). */
export function fireStatusLabel(
  status: FireStatus,
  t: ScheduleTranslator,
): string {
  switch (status) {
    case "ran":
      return t("fireRan");
    case "ran_late":
      return t("fireRanLate");
    case "missed":
      return t("fireMissed");
  }
}

// --- month / week grid layout (pure calendar arithmetic; the presentation views bind to it) ---

export interface DayCell {
  /** The calendar date key (YYYY-MM-DD) — aligns with an occurrence's display-tz day key. */
  day: string;
  /** Whether this cell's date belongs to the grid's focus month (false = a leading/trailing day). */
  inMonth: boolean;
}

const _MS_DAY = 86_400_000;

function isoDay(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
}

/**
 * The 6×7 Monday-start calendar grid for a month (`month0` is 0-based) — day keys + in-month flags
 * only. Pure calendar arithmetic; the month view maps occurrences onto the cells by day key (the
 * same keys `groupByDay` produces), so no recurrence math ever reaches the grid.
 */
export function monthGridWeeks(year: number, month0: number): DayCell[][] {
  const first = new Date(Date.UTC(year, month0, 1));
  const offset = (first.getUTCDay() + 6) % 7; // Monday-start (getUTCDay: 0=Sun..6=Sat)
  const start = Date.UTC(year, month0, 1 - offset);
  const weeks: DayCell[][] = [];
  for (let w = 0; w < 6; w++) {
    const week: DayCell[] = [];
    for (let d = 0; d < 7; d++) {
      const cell = new Date(start + (w * 7 + d) * _MS_DAY);
      week.push({ day: isoDay(cell), inMonth: cell.getUTCMonth() === month0 });
    }
    weeks.push(week);
  }
  return weeks;
}

/** The 7 Monday-start day keys of the week containing `anchor` (the week view's columns). */
export function weekDays(anchor: Date): string[] {
  const offset = (anchor.getUTCDay() + 6) % 7;
  const monday =
    Date.UTC(
      anchor.getUTCFullYear(),
      anchor.getUTCMonth(),
      anchor.getUTCDate(),
    ) -
    offset * _MS_DAY;
  return Array.from({ length: 7 }, (_v, i) =>
    isoDay(new Date(monday + i * _MS_DAY)),
  );
}

/** Occurrences keyed by their display-tz calendar day — for O(1) cell lookup in the grids. */
export function occurrencesByDay(
  occurrences: Occurrence[],
  tz: string,
): Map<string, Occurrence[]> {
  const map = new Map<string, Occurrence[]>();
  for (const g of groupByDay(occurrences, tz)) map.set(g.day, g.items);
  return map;
}

/** Fire-history statuses keyed by display-tz calendar day — the grids' honest ran/missed markers. */
export function historyByDay(
  history: FireEvent[],
  tz: string,
): Map<string, FireStatus[]> {
  const map = new Map<string, FireStatus[]>();
  for (const ev of history) {
    const key = new Intl.DateTimeFormat("en-CA", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date(ev.at));
    const bucket = map.get(key);
    if (bucket) bucket.push(ev.status);
    else map.set(key, [ev.status]);
  }
  return map;
}
