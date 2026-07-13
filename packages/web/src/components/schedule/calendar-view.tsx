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
 *
 * R11-B3 — the persona-web v3 register (ui_kits 3 schedule.html): mono kicker + Fraunces title,
 * the WHOLE agenda row is the reschedule door (identity rail, mono time, executor who-line,
 * chevron), a mono dow header + today ring + separated cells on the grids, week chips carry
 * time+terms while month chips clamp to three with an honest "+N more", and the dialogs are
 * titled panels. Same engine, same doors — only the register changed.
 */

import { ChevronRight, Info } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useAuth } from "@/auth";
import { useConfirm } from "@/components/providers/confirm-provider";
import { useNotify } from "@/components/providers/notification-provider";
import { Button } from "@/components/ui/button";
import {
  applyReschedule,
  deleteSchedule,
  fetchOccurrences,
  previewReschedule,
  type ReschedulePreview,
  ScheduleApiError,
} from "@/lib/api/schedule-client";
import { useSidebarRefresh } from "@/lib/hooks/use-sidebar-refresh";
import { personaIdentityStyle } from "@/lib/persona-identity";
import {
  dayKey,
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
const MONTH_CELL_CAP = 3;
type View = "agenda" | "week" | "month";

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;

const _cellStyle = (occ: Occurrence) =>
  occ.persona_id ? personaIdentityStyle({ id: occ.persona_id }) : undefined;

export interface CalendarViewProps {
  /** The owner's personas — the create dialog's executor picker (Spec A10, A10-D-3)
   * AND the agenda rows' who-line (R11-B3). */
  personas?: ReminderPersona[];
  /** The profile timezone the create flow anchors to (browser tz when unset). */
  defaultTimezone?: string | null;
  /**
   * R9-024: scope the calendar to one persona's schedules — the chat right-panel instance.
   * Unset (the `/schedule` page) shows every owned schedule, exactly as before (the
   * server-side filter is additive-optional — same component, no fork).
   */
  personaId?: string;
}

export function CalendarView({
  personas = [],
  defaultTimezone,
  personaId,
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
      setData(await fetchOccurrences(await getToken(), from, to, personaId));
      setError(null);
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Failed to load your schedule.",
      );
    }
  }, [getToken, from, to, personaId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) return <p className="v-schedule-error">{error}</p>;
  if (!data)
    return <p className="v-schedule-loading">Loading your schedule…</p>;

  const notice = truncationNotice(data, DISPLAY_TZ);
  const byDay = occurrencesByDay(data.occurrences, DISPLAY_TZ);
  const hist = historyByDay(data.history, DISPLAY_TZ);
  const today = dayKey(from.toISOString(), DISPLAY_TZ);
  const nameOf = (id: string | null) =>
    id ? (personas.find((p) => p.id === id)?.name ?? null) : null;

  return (
    <section className="v-schedule">
      <header className="v-schedule-head">
        <div>
          <p className="v-schedule-kicker">Schedule</p>
          <h1>Calendar</h1>
        </div>
        <span className="v-schedule-tz">Times in {DISPLAY_TZ}</span>
      </header>

      <div className="v-schedule-toolbar">
        {/* Spec A10 (T5): the user's direct create door (A10-D-9 — preview→confirm IS
            the one explicit confirmation; the write path stays A8's ScheduleStore). */}
        <Button type="button" onClick={() => setCreating(true)}>
          New routine
        </Button>
        <div className="v-schedule-views">
          {(["agenda", "week", "month"] as const).map((v) => (
            <button
              key={v}
              type="button"
              aria-pressed={view === v}
              onClick={() => setView(v)}
            >
              {v[0].toUpperCase() + v.slice(1)}
            </button>
          ))}
        </div>
      </div>

      {/* The honest truncation banner — shown in every view, never an infinite calendar. */}
      {notice && (
        <output className="v-schedule-truncation">
          <Info className="size-4 shrink-0" aria-hidden="true" />
          {notice}
        </output>
      )}

      {view === "agenda" && (
        <AgendaView
          data={data}
          hist={hist}
          nameOf={nameOf}
          onEdit={setEditing}
          onCreate={() => setCreating(true)}
        />
      )}
      {view === "week" && (
        <GridView
          variant="week"
          cells={weekDays(from)}
          byDay={byDay}
          hist={hist}
          today={today}
          onEdit={setEditing}
        />
      )}
      {view === "month" && (
        <GridView
          variant="month"
          cells={monthGridWeeks(from.getFullYear(), from.getMonth()).flat()}
          byDay={byDay}
          hist={hist}
          today={today}
          onEdit={setEditing}
        />
      )}

      {editing && (
        <RescheduleDialog
          occurrence={editing}
          personaName={nameOf(editing.persona_id)}
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

/** One agenda row — the WHOLE row opens the reschedule twin (kit `.occ`). */
function OccurrenceRow({
  occ,
  name,
  onEdit,
}: {
  occ: Occurrence;
  name: string | null;
  onEdit: (o: Occurrence) => void;
}) {
  return (
    <button
      type="button"
      className="v-occurrence-card"
      style={_cellStyle(occ)}
      onClick={() => onEdit(occ)}
      aria-label={`Reschedule: ${occ.human_terms}`}
    >
      <span className="v-occurrence-time">
        {occurrenceTime(occ, DISPLAY_TZ)}
      </span>
      <span className="v-occurrence-body">
        <span className="v-occurrence-terms">{occ.human_terms}</span>
        {name ? (
          <span className="v-occurrence-who">
            <span className="v-iddot" aria-hidden="true" />
            {name}
          </span>
        ) : null}
      </span>
      <ChevronRight className="v-occurrence-chev size-4" aria-hidden="true" />
    </button>
  );
}

function AgendaView({
  data,
  hist,
  nameOf,
  onEdit,
  onCreate,
}: {
  data: OccurrencesResult;
  hist: Map<string, FireStatus[]>;
  nameOf: (id: string | null) => string | null;
  onEdit: (o: Occurrence) => void;
  onCreate: () => void;
}) {
  const groups = groupByDay(data.occurrences, DISPLAY_TZ);
  if (groups.length === 0)
    return (
      <p className="v-schedule-empty">
        Nothing scheduled in this window.{" "}
        <Button type="button" variant="outline" onClick={onCreate}>
          Create your first routine
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
            <OccurrenceRow
              key={`${occ.schedule_id}-${occ.fire_at}`}
              occ={occ}
              name={nameOf(occ.persona_id)}
              onEdit={onEdit}
            />
          ))}
        </div>
      ))}
    </>
  );
}

/** The week + month grids: pure layout over the day-keyed occurrences + history.
 * Week chips carry time + terms (tall cells); month chips clamp to three with an
 * honest "+N more" (kit behaviour — never a silently-overflowing cell). */
function GridView({
  variant,
  cells,
  byDay,
  hist,
  today,
  onEdit,
}: {
  variant: "week" | "month";
  cells: string[] | { day: string; inMonth: boolean }[];
  byDay: Map<string, Occurrence[]>;
  hist: Map<string, FireStatus[]>;
  today: string;
  onEdit: (o: Occurrence) => void;
}) {
  const normalized = cells.map((c) =>
    typeof c === "string" ? { day: c, inMonth: true } : c,
  );
  return (
    <div>
      <div className="v-dowhead" aria-hidden="true">
        {DOW.map((d) => (
          <span key={d}>{d}</span>
        ))}
      </div>
      <div className={`v-grid v-grid--${variant}`}>
        {normalized.map(({ day, inMonth }) => {
          const items = byDay.get(day) ?? [];
          const shown =
            variant === "month" ? items.slice(0, MONTH_CELL_CAP) : items;
          const more = items.length - shown.length;
          return (
            <div
              key={day}
              className={`v-grid-cell${inMonth ? "" : " v-grid-cell--muted"}${
                day === today ? " v-grid-cell--today" : ""
              }`}
            >
              <div className="v-grid-daynum">
                {Number.parseInt(day.slice(-2), 10)}
                <DayStatus statuses={hist.get(day)} />
              </div>
              {shown.map((occ) => (
                <button
                  key={`${occ.schedule_id}-${occ.fire_at}`}
                  type="button"
                  className="v-grid-occ"
                  style={_cellStyle(occ)}
                  onClick={() => onEdit(occ)}
                  title={occ.human_terms}
                >
                  {occurrenceTime(occ, DISPLAY_TZ)}
                  {variant === "week" ? ` ${occ.human_terms}` : null}
                </button>
              ))}
              {more > 0 ? (
                <span className="v-grid-more">+{more} more</span>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

interface RescheduleDialogProps {
  occurrence: Occurrence;
  personaName: string | null;
  onClose: () => void;
  onApplied: () => Promise<void>;
}

/** The calendar's edit twin: build cadence → preview (engine) → confirm → apply (same door).
 * Inherits the one-time kind from the shared builder (Spec A10, A10-D-5) for free.
 *
 * R9-024: also the delete affordance. Every write (preview/apply/delete) is caught — a 409
 * (`schedule_state_conflict`, R9-023's fired-one-time re-arm guard) surfaces as a named
 * conflict toast; any other failure surfaces a generic one. Never an uncaught rejection. */
function RescheduleDialog({
  occurrence,
  personaName,
  onClose,
  onApplied,
}: RescheduleDialogProps) {
  const { getToken } = useAuth();
  const { notify } = useNotify();
  const confirm = useConfirm();
  const refreshSidebar = useSidebarRefresh();
  const t = useTranslations("schedule.calendar");
  const tc = useTranslations("confirm");
  const [cadence, setCadence] = useState<CadenceInput | null>(null);
  const [preview, setPreview] = useState<ReschedulePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const tz = occurrence.timezone;

  const surfaceFailure = useCallback(
    (e: unknown, fallback: string) => {
      if (
        e instanceof ScheduleApiError &&
        e.code === "schedule_state_conflict"
      ) {
        notify({
          level: "error",
          title: t("conflictTitle"),
          body: t("conflictBody"),
        });
        return;
      }
      notify({ level: "error", title: fallback });
    },
    [notify, t],
  );

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
    } catch (e) {
      surfaceFailure(e, t("previewFailed"));
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
    } catch (e) {
      surfaceFailure(e, t("rescheduleFailed"));
      setBusy(false);
    }
  }

  async function doDelete() {
    const ok = await confirm({
      title: t("deleteConfirmTitle"),
      description: t("deleteConfirmBody"),
      confirmLabel: tc("delete"),
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await deleteSchedule(await getToken(), occurrence.schedule_id);
      refreshSidebar(); // R9-012: the Schedule badge reflects the removed row
      await onApplied();
    } catch (e) {
      surfaceFailure(e, t("deleteFailed"));
      setBusy(false);
    }
  }

  return (
    <div
      className="v-reschedule-dialog"
      role="dialog"
      aria-label="Reschedule"
      style={_cellStyle(occurrence)}
    >
      <h2 className="v-dialog-title">Reschedule</h2>
      <p className="v-dialog-sub">
        {occurrence.persona_id ? (
          <span className="v-iddot" aria-hidden="true" />
        ) : null}
        {personaName ? `${personaName} · ` : null}
        {occurrence.human_terms}
      </p>
      <RecurrenceBuilder timezone={tz} onChange={setCadence} />
      {/* The confirm echo — the SAME full clause chat re-echoes, from the engine preview. */}
      {preview && (
        <p className="v-reschedule-preview">
          <b>When:</b> {preview.human_terms} · {preview.timezone}
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
        <Button
          type="button"
          variant="destructive"
          onClick={doDelete}
          disabled={busy}
        >
          {t("deleteButton")}
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
