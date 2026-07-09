"use client";

/**
 * Spec A8 (T8/T9) — the calendar surface: agenda / week / month over the occurrences API.
 *
 * All three views render ONLY what the occurrences API returns (the one engine, criterion 5) — no
 * client-side recurrence math. The month/week grids are pure LAYOUT over the same helpers
 * (`occurrencesByDay` / `historyByDay` / `monthGridWeeks` / `weekDays`, unit-tested). Truncation is
 * shown honestly (bar 2) and fire-status markers render in every view (bar 6). A reschedule opens
 * the twin of the chat verb: pick a cadence → PREVIEW (the engine's next-fire + full clause +
 * quiet-hours warn) → confirm → apply through the SAME CAS door (bar 4).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useAuth } from "@/auth";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  applyReschedule,
  fetchOccurrences,
  previewReschedule,
  type ReschedulePreview,
} from "@/lib/api/schedule-client";
import { useSidebarRefresh } from "@/lib/hooks/use-sidebar-refresh";
import { personaIdentityStyle } from "@/lib/persona-identity";
import {
  type FireStatus,
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
} from "@/lib/schedule/agenda";
import {
  CreateReminderDialog,
  type ReminderPersona,
} from "./create-reminder-dialog";
import { type CadenceInput, RecurrenceBuilder } from "./recurrence-builder";

const DISPLAY_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
const WINDOW_DAYS = 45;
type View = "agenda" | "week" | "month";

const _cellStyle = (occ: Occurrence) =>
  occ.persona_id ? personaIdentityStyle({ id: occ.persona_id }) : undefined;

export interface CalendarViewProps {
  /** The owner's personas — the create dialog's executor picker (Spec A10, A10-D-3). */
  personas?: ReminderPersona[];
  /** The profile timezone the create flow anchors to (browser tz when unset). */
  defaultTimezone?: string | null;
}

export function CalendarView({
  personas = [],
  defaultTimezone,
}: CalendarViewProps) {
  const { getToken } = useAuth();
  const refreshSidebar = useSidebarRefresh();
  const [data, setData] = useState<OccurrencesResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Occurrence | null>(null);
  const [creating, setCreating] = useState(false);
  const [view, setView] = useState<View>("agenda");
  const createTz = defaultTimezone || DISPLAY_TZ;

  const [from, to] = useMemo(() => {
    const now = new Date();
    const end = new Date(now.getTime() + WINDOW_DAYS * 86_400_000);
    return [now, end] as const;
  }, []);

  const load = useCallback(async () => {
    try {
      setData(await fetchOccurrences(await getToken(), from, to));
      setError(null);
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Failed to load your schedule.",
      );
    }
  }, [getToken, from, to]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) return <p className="v-schedule-error">{error}</p>;
  if (!data)
    return <p className="v-schedule-loading">Loading your schedule…</p>;

  const notice = truncationNotice(data, DISPLAY_TZ);
  const byDay = occurrencesByDay(data.occurrences, DISPLAY_TZ);
  const hist = historyByDay(data.history, DISPLAY_TZ);

  return (
    <section className="v-schedule">
      <header className="v-schedule-head">
        <h1>Calendar</h1>
        <span className="v-schedule-tz">Times shown in {DISPLAY_TZ}</span>
        {/* Spec A10 (T5): the user's direct create door (A10-D-9 — preview→confirm IS
            the one explicit confirmation; the write path stays A8's ScheduleStore). */}
        <Button type="button" onClick={() => setCreating(true)}>
          New reminder
        </Button>
        <div className="v-schedule-views">
          {(["agenda", "week", "month"] as const).map((v) => (
            <Button
              key={v}
              type="button"
              variant={view === v ? "default" : "outline"}
              aria-pressed={view === v}
              onClick={() => setView(v)}
            >
              {v[0].toUpperCase() + v.slice(1)}
            </Button>
          ))}
        </div>
      </header>

      {/* The honest truncation banner — shown in every view, never an infinite calendar. */}
      {notice && <output className="v-schedule-truncation">{notice}</output>}

      {view === "agenda" && (
        <AgendaView
          data={data}
          hist={hist}
          onEdit={setEditing}
          onCreate={() => setCreating(true)}
        />
      )}
      {view === "week" && (
        <GridView
          cells={weekDays(from)}
          byDay={byDay}
          hist={hist}
          onEdit={setEditing}
        />
      )}
      {view === "month" && (
        <GridView
          cells={monthGridWeeks(from.getFullYear(), from.getMonth()).flat()}
          byDay={byDay}
          hist={hist}
          onEdit={setEditing}
        />
      )}

      {editing && (
        <RescheduleDialog
          occurrence={editing}
          onClose={() => setEditing(null)}
          onApplied={async () => {
            setEditing(null);
            await load();
          }}
        />
      )}

      {creating && (
        <CreateReminderDialog
          personas={personas}
          defaultTimezone={createTz}
          onClose={() => setCreating(false)}
          onCreated={async () => {
            setCreating(false);
            await load(); // the new occurrence appears immediately — same engine read
            // R9-012: the shared sidebar-refresh seam — the Schedule badge
            // re-resolves server-side (soft refresh; calendar state preserved).
            refreshSidebar();
          }}
        />
      )}
    </section>
  );
}

/** A small honest fire-status marker for a day (ran / ran-late / missed), or nothing. */
function DayStatus({ statuses }: { statuses: FireStatus[] | undefined }) {
  if (!statuses || statuses.length === 0) return null;
  const worst: FireStatus = statuses.includes("missed")
    ? "missed"
    : statuses.includes("ran_late")
      ? "ran_late"
      : "ran";
  return (
    <span className={`v-day-status v-day-status--${worst}`}>
      {fireStatusLabel(worst)}
    </span>
  );
}

function AgendaView({
  data,
  hist,
  onEdit,
  onCreate,
}: {
  data: OccurrencesResult;
  hist: Map<string, FireStatus[]>;
  onEdit: (o: Occurrence) => void;
  onCreate: () => void;
}) {
  const groups = groupByDay(data.occurrences, DISPLAY_TZ);
  if (groups.length === 0)
    return (
      <p className="v-schedule-empty">
        Nothing scheduled in this window.{" "}
        <Button type="button" variant="outline" onClick={onCreate}>
          Create your first reminder
        </Button>
      </p>
    );
  return (
    <>
      {groups.map((group) => (
        <div key={group.day} className="v-agenda-day">
          <h2 className="v-agenda-heading">
            {group.heading} <DayStatus statuses={hist.get(group.day)} />
          </h2>
          {group.items.map((occ) => (
            <Card
              key={`${occ.schedule_id}-${occ.fire_at}`}
              className="v-occurrence-card"
              style={_cellStyle(occ)}
            >
              <CardHeader>
                <CardTitle>{occurrenceTime(occ, DISPLAY_TZ)}</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="v-occurrence-terms">{occ.human_terms}</p>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => onEdit(occ)}
                >
                  Reschedule
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      ))}
    </>
  );
}

/** The week + month grids: pure layout over the day-keyed occurrences + history. */
function GridView({
  cells,
  byDay,
  hist,
  onEdit,
}: {
  cells: string[] | { day: string; inMonth: boolean }[];
  byDay: Map<string, Occurrence[]>;
  hist: Map<string, FireStatus[]>;
  onEdit: (o: Occurrence) => void;
}) {
  const normalized = cells.map((c) =>
    typeof c === "string" ? { day: c, inMonth: true } : c,
  );
  return (
    <div className="v-grid">
      {normalized.map(({ day, inMonth }) => {
        const items = byDay.get(day) ?? [];
        return (
          <div
            key={day}
            className={`v-grid-cell${inMonth ? "" : " v-grid-cell--muted"}`}
          >
            <div className="v-grid-daynum">
              {Number.parseInt(day.slice(-2), 10)}
              <DayStatus statuses={hist.get(day)} />
            </div>
            {items.map((occ) => (
              <button
                key={`${occ.schedule_id}-${occ.fire_at}`}
                type="button"
                className="v-grid-occ"
                style={_cellStyle(occ)}
                onClick={() => onEdit(occ)}
                title={occ.human_terms}
              >
                {occurrenceTime(occ, DISPLAY_TZ)}
              </button>
            ))}
          </div>
        );
      })}
    </div>
  );
}

interface RescheduleDialogProps {
  occurrence: Occurrence;
  onClose: () => void;
  onApplied: () => Promise<void>;
}

/** The calendar's edit twin: build cadence → preview (engine) → confirm → apply (same door).
 * Inherits the one-time kind from the shared builder (Spec A10, A10-D-5) for free. */
function RescheduleDialog({
  occurrence,
  onClose,
  onApplied,
}: RescheduleDialogProps) {
  const { getToken } = useAuth();
  const [cadence, setCadence] = useState<CadenceInput | null>(null);
  const [preview, setPreview] = useState<ReschedulePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const tz = occurrence.timezone;

  async function doPreview() {
    if (!cadence) return;
    setBusy(true);
    try {
      setPreview(
        await previewReschedule(await getToken(), occurrence.schedule_id, {
          pattern: cadence.pattern,
          one_time_at: cadence.one_time_at,
          timezone: tz,
        }),
      );
    } finally {
      setBusy(false);
    }
  }

  async function doApply() {
    if (!cadence) return;
    setBusy(true);
    try {
      await applyReschedule(await getToken(), occurrence.schedule_id, {
        pattern: cadence.pattern,
        one_time_at: cadence.one_time_at,
        timezone: tz,
      });
      await onApplied();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="v-reschedule-dialog" role="dialog" aria-label="Reschedule">
      <RecurrenceBuilder timezone={tz} onChange={setCadence} />
      {/* The confirm echo — the SAME full clause chat re-echoes, from the engine preview. */}
      {preview && (
        <p className="v-reschedule-preview">
          When: {preview.human_terms} · {preview.timezone}
          {preview.next_fire &&
            ` — next run ${new Intl.DateTimeFormat(undefined, {
              timeZone: tz,
              weekday: "short",
              day: "numeric",
              month: "short",
            }).format(new Date(preview.next_fire))}`}
          {preview.quiet_hours_offer &&
            ` (that's in your quiet hours — ${preview.quiet_hours_offer} instead?)`}
        </p>
      )}
      <div className="v-reschedule-actions">
        <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        {preview ? (
          <Button type="button" onClick={doApply} disabled={busy || !cadence}>
            Confirm
          </Button>
        ) : (
          <Button type="button" onClick={doPreview} disabled={busy || !cadence}>
            Preview
          </Button>
        )}
      </div>
    </div>
  );
}
