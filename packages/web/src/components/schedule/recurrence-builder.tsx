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
 */

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { RecurrencePatternInput } from "@/lib/api/schedule-client";

const WEEKDAYS: readonly { token: string; label: string }[] = [
  { token: "MO", label: "Mon" },
  { token: "TU", label: "Tue" },
  { token: "WE", label: "Wed" },
  { token: "TH", label: "Thu" },
  { token: "FR", label: "Fri" },
  { token: "SA", label: "Sat" },
  { token: "SU", label: "Sun" },
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
}

/** A controlled recurrence + time picker; emits picker-state (never a raw RRULE). */
export function RecurrenceBuilder({
  timezone,
  onChange,
}: RecurrenceBuilderProps) {
  const [kind, setKind] = useState<Kind>("daily");
  const [interval, setInterval] = useState(1);
  const [weekdays, setWeekdays] = useState<string[]>(["MO"]);
  const [monthDay, setMonthDay] = useState(1);
  const [time, setTime] = useState("09:00");
  const [onceAt, setOnceAt] = useState("");

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
  // biome-ignore lint/correctness/useExhaustiveDependencies: emit-once-on-mount is intentional.
  useEffect(() => {
    emit({});
  }, []);

  function toggleWeekday(token: string) {
    const next = weekdays.includes(token)
      ? weekdays.filter((t) => t !== token)
      : [
          ...WEEKDAYS.map((w) => w.token).filter(
            (t) => weekdays.includes(t) || t === token,
          ),
        ];
    if (next.length === 0) return; // weekly needs at least one day
    setWeekdays(next);
    emit({ weekdays: next });
  }

  return (
    <div className="v-recur-builder" data-testid="recurrence-builder">
      <label htmlFor="recur-kind">
        Repeats
        <select
          id="recur-kind"
          value={kind}
          onChange={(e) => {
            const k = e.target.value as Kind;
            setKind(k);
            emit({ kind: k });
          }}
        >
          <option value="daily">Every day</option>
          <option value="weekly">Weekly on…</option>
          <option value="monthly_day">Monthly on a date</option>
          <option value="hourly">Every N hours</option>
          <option value="once">Once, at…</option>
        </select>
      </label>

      {kind === "once" && (
        <label htmlFor="recur-once">
          At
          <Input
            id="recur-once"
            type="datetime-local"
            value={onceAt}
            onChange={(e) => {
              setOnceAt(e.target.value);
              emit({ onceAt: e.target.value });
            }}
          />
          <span className="v-recur-tz">
            (your local time — shown on the calendar in {timezone})
          </span>
        </label>
      )}

      {kind === "hourly" && (
        <label htmlFor="recur-interval">
          Every
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
          hours (wall-clock — at these local times, {timezone})
        </label>
      )}

      {kind !== "hourly" && kind !== "once" && (
        <label htmlFor="recur-time">
          At
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
          <legend>Weekdays</legend>
          {WEEKDAYS.map((w) => (
            <Button
              key={w.token}
              type="button"
              variant={weekdays.includes(w.token) ? "default" : "outline"}
              aria-pressed={weekdays.includes(w.token)}
              onClick={() => toggleWeekday(w.token)}
            >
              {w.label}
            </Button>
          ))}
        </fieldset>
      )}

      {kind === "monthly_day" && (
        <label htmlFor="recur-monthday">
          On day
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
          <span>(-1 = the last day of the month)</span>
        </label>
      )}
    </div>
  );
}
