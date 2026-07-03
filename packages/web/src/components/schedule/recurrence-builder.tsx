"use client";

/**
 * Spec A8 (T9) — the humane recurrence builder + time picker.
 *
 * Builds a `RecurrencePatternInput` (the humane vocabulary, A8-D-1) — the client NEVER produces a
 * raw RRULE; the server maps pattern → rule (bar 1). The timezone is shown explicitly (bar 3).
 *
 * F2 GAP (A8-D-5): there is no shared F2 calendar/time-picker primitive yet. This composes the
 * minimal in-house control from existing primitives (`<input type="time">`, a native `<select>`,
 * weekday toggle buttons) — no new dependency. FLAGGED to F2 as a shared-primitive need; when F2
 * ships one, this swaps behind the same props.
 */

import { useState } from "react";

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

type Kind = RecurrencePatternInput["kind"];

export interface RecurrenceBuilderProps {
  timezone: string;
  onChange: (pattern: RecurrencePatternInput) => void;
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

  function emit(
    next: Partial<{
      kind: Kind;
      interval: number;
      weekdays: string[];
      monthDay: number;
      time: string;
    }>,
  ) {
    const k = next.kind ?? kind;
    const iv = next.interval ?? interval;
    const wd = next.weekdays ?? weekdays;
    const md = next.monthDay ?? monthDay;
    const [h, m] = (next.time ?? time)
      .split(":")
      .map((s) => Number.parseInt(s, 10));
    const base = { hour: h, minute: m, interval: iv };
    if (k === "weekly") onChange({ kind: "weekly", weekdays: wd, ...base });
    else if (k === "monthly_day")
      onChange({ kind: "monthly_day", month_day: md, ...base });
    else if (k === "hourly")
      onChange({ kind: "hourly", interval: iv, minute: m });
    else onChange({ kind: "daily", ...base });
  }

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
        </select>
      </label>

      {kind === "hourly" ? (
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
      ) : (
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
