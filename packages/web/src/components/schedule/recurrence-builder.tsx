"use client";

/**
 * Spec A8 (T9) — the humane recurrence builder + time picker.
 * Spec A10 (A10-D-5) — the additive "Once, at…" kind: the builder now emits a CADENCE
 * (pattern XOR one_time_at) so one-time reminders are expressible from the same picker;
 * the reschedule dialog inherits one-time re-timing for free.
 *
 * Builds a `CadenceInput` (the humane vocabulary, A8-D-1) — the client NEVER produces a
 * raw RRULE; the server maps pattern → rule (bar 1). The timezone is shown explicitly (bar 3).
 *
 * F2 GAP (A8-D-5): there is no shared F2 calendar/time-picker primitive yet. This composes the
 * minimal in-house control from existing primitives (`<input type="time">`, a native `<select>`,
 * weekday toggle buttons) — no new dependency. FLAGGED to F2 as a shared-primitive need; when F2
 * ships one, this swaps behind the same props.
 *
 * R9-178: `initial` seeds the picker with a schedule's CURRENT cadence (the API's
 * `RecurrencePattern` read back through `rule_to_pattern`), so a reschedule opens on what is
 * really set instead of the "Every day, 09:00" default. Absent, the create dialog is unchanged.
 */

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { Input } from "@/components/ui/input";
import type { RecurrencePatternInput } from "@/lib/api/schedule-client";

/** RRULE weekday tokens; the visible label comes from
 * `schedule.recurrence.weekday.<token>` so it translates with the locale. */
const WEEKDAYS: readonly string[] = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];

/** The cadence chip row (kit `.chips`) — the humane vocabulary, one chip each.
 * Labels live in `schedule.recurrence.kind.<kind>`. */
const KINDS: readonly string[] = [
  "once",
  "daily",
  "weekly",
  "monthly_day",
  "hourly",
];

/** The builder's output: exactly one of a recurring pattern or a one-time instant (ISO UTC). */
export interface CadenceInput {
  pattern: RecurrencePatternInput | null;
  one_time_at: string | null;
}

type Kind = RecurrencePatternInput["kind"] | "once";

export interface RecurrenceBuilderProps {
  timezone: string;
  onChange: (cadence: CadenceInput) => void;
  /** The cadence to open on (R9-178). Absent or unseedable, the builder opens on its default. */
  initial?: CadenceInput | null;
}

/** The picker's internal state, as one seedable value. */
interface PickerState {
  kind: Kind;
  interval: number;
  weekdays: string[];
  monthDay: number;
  time: string;
  onceAt: string;
}

const DEFAULT_STATE: PickerState = {
  kind: "daily",
  interval: 1,
  weekdays: ["MO"],
  monthDay: 1,
  time: "09:00",
  onceAt: "",
};

/**
 * Whether the builder can open on this cadence: a one-time instant, or a pattern of a kind it
 * has a chip for. A pattern of another kind (monthly-by-weekday, yearly) and an absent cadence
 * both read as "cannot show it", which the dialogs turn into a Replace warning.
 */
export function canSeedBuilder(
  cadence: CadenceInput | null | undefined,
): boolean {
  if (!cadence) return false;
  if (cadence.one_time_at) return true;
  return cadence.pattern !== null && KINDS.includes(cadence.pattern.kind);
}

const pad = (n: number) => String(n).padStart(2, "0");

/** An ISO instant as the browser-local `datetime-local` value (the inverse of emit's Date). */
function toLocalInput(iso: string): string {
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`;
}

/** Picker state for a cadence the builder can seed from, else the default. */
function seedState(initial: CadenceInput | null | undefined): PickerState {
  if (!initial || !canSeedBuilder(initial)) return DEFAULT_STATE;
  if (initial.one_time_at) {
    return {
      ...DEFAULT_STATE,
      kind: "once",
      onceAt: toLocalInput(initial.one_time_at),
    };
  }
  const p = initial.pattern as RecurrencePatternInput; // canSeedBuilder proved it
  return {
    kind: p.kind,
    interval: p.interval ?? 1,
    weekdays: p.weekdays?.length ? [...p.weekdays] : DEFAULT_STATE.weekdays,
    monthDay: p.month_day ?? DEFAULT_STATE.monthDay,
    // hourly carries no hour; its minute still rides in the time field for emit.
    time: `${pad(p.hour ?? 0)}:${pad(p.minute ?? 0)}`,
    onceAt: "",
  };
}

/** A controlled recurrence + time picker; emits picker-state (never a raw RRULE). */
export function RecurrenceBuilder({
  timezone,
  onChange,
  initial,
}: RecurrenceBuilderProps) {
  const t = useTranslations("schedule.recurrence");
  const [seed] = useState(() => seedState(initial));
  const [kind, setKind] = useState<Kind>(seed.kind);
  const [interval, setInterval] = useState(seed.interval);
  const [weekdays, setWeekdays] = useState<string[]>(seed.weekdays);
  const [monthDay, setMonthDay] = useState(seed.monthDay);
  const [time, setTime] = useState(seed.time);
  const [onceAt, setOnceAt] = useState(seed.onceAt);

  function emit(
    next: Partial<{
      kind: Kind;
      interval: number;
      weekdays: string[];
      monthDay: number;
      time: string;
      onceAt: string;
    }>,
  ) {
    const k = next.kind ?? kind;
    const iv = next.interval ?? interval;
    const wd = next.weekdays ?? weekdays;
    const md = next.monthDay ?? monthDay;
    if (k === "once") {
      const raw = next.onceAt ?? onceAt;
      // datetime-local is the BROWSER's wall clock; Date converts it to the absolute
      // instant. A one-time is an instant — the server renders it in the schedule tz.
      const instant = raw ? new Date(raw).toISOString() : null;
      onChange({ pattern: null, one_time_at: instant });
      return;
    }
    const [h, m] = (next.time ?? time)
      .split(":")
      .map((s) => Number.parseInt(s, 10));
    const base = { hour: h, minute: m, interval: iv };
    let pattern: RecurrencePatternInput;
    if (k === "weekly") pattern = { kind: "weekly", weekdays: wd, ...base };
    else if (k === "monthly_day")
      pattern = { kind: "monthly_day", month_day: md, ...base };
    else if (k === "hourly")
      pattern = { kind: "hourly", interval: iv, minute: m };
    else pattern = { kind: "daily", ...base };
    onChange({ pattern, one_time_at: null });
  }

  // R4-C1-24: emit the VISIBLE default cadence ("Every day at 09:00") on mount so a
  // user who accepts the shown default without touching the picker still satisfies the
  // parent's ``cadence !== null`` gate — otherwise Preview/Create stayed dead until the
  // picker was touched. The picker owns its default and announces it (single source).
  // R9-178: with `initial`, the same emit announces the SEEDED cadence, so an untouched
  // Apply re-applies what is set rather than the default.
  // biome-ignore lint/correctness/useExhaustiveDependencies: emit-once-on-mount is intentional.
  useEffect(() => {
    emit({});
  }, []);

  function toggleWeekday(token: string) {
    const next = weekdays.includes(token)
      ? weekdays.filter((d) => d !== token)
      : [...WEEKDAYS.filter((d) => weekdays.includes(d) || d === token)];
    if (next.length === 0) return; // weekly needs at least one day
    setWeekdays(next);
    emit({ weekdays: next });
  }

  return (
    <div className="v-recur-builder" data-testid="recurrence-builder">
      {/* R11-B3: the kit's cadence chips replace the select — same kinds, same emit. */}
      <fieldset className="v-recur-chips">
        <legend className="v-recur-label">{t("cadence")}</legend>
        {KINDS.map((k) => (
          <button
            key={k}
            type="button"
            aria-pressed={kind === k}
            onClick={() => {
              setKind(k as Kind);
              emit({ kind: k as Kind });
            }}
          >
            {t(`kind.${k}`)}
          </button>
        ))}
      </fieldset>

      {kind === "once" && (
        <label htmlFor="recur-once">
          {t("at")}
          <Input
            id="recur-once"
            type="datetime-local"
            value={onceAt}
            onChange={(e) => {
              setOnceAt(e.target.value);
              emit({ onceAt: e.target.value });
            }}
          />
          <span className="v-recur-tz">{t("localTimeHint", { timezone })}</span>
        </label>
      )}

      {kind === "hourly" && (
        <label htmlFor="recur-interval">
          {t("every")}
          <Input
            id="recur-interval"
            type="number"
            min={1}
            max={24}
            value={interval}
            onChange={(e) => {
              const iv = Number.parseInt(e.target.value, 10) || 1;
              setInterval(iv);
              emit({ interval: iv });
            }}
          />
          {t("hoursSuffix", { timezone })}
        </label>
      )}

      {kind !== "hourly" && kind !== "once" && (
        <label htmlFor="recur-time">
          {t("at")}
          <Input
            id="recur-time"
            type="time"
            value={time}
            onChange={(e) => {
              setTime(e.target.value);
              emit({ time: e.target.value });
            }}
          />
          <span className="v-recur-tz">{timezone}</span>
        </label>
      )}

      {kind === "weekly" && (
        <fieldset className="v-recur-weekdays">
          <legend>{t("on")}</legend>
          {WEEKDAYS.map((token) => {
            const label = t(`weekday.${token}`);
            return (
              <button
                key={token}
                type="button"
                aria-pressed={weekdays.includes(token)}
                aria-label={label}
                title={label}
                onClick={() => toggleWeekday(token)}
              >
                {label[0]}
              </button>
            );
          })}
        </fieldset>
      )}

      {kind === "monthly_day" && (
        <label htmlFor="recur-monthday">
          {t("onDay")}
          <Input
            id="recur-monthday"
            type="number"
            min={-1}
            max={31}
            value={monthDay}
            onChange={(e) => {
              const md = Number.parseInt(e.target.value, 10) || 1;
              setMonthDay(md);
              emit({ monthDay: md });
            }}
          />
          <span>{t("lastDayHint")}</span>
        </label>
      )}
    </div>
  );
}
