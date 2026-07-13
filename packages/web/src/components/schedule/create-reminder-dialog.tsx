"use client";

/**
 * Spec A10 (T5) — the "New routine" flow (R11-B3 naming): the user's direct create door on the calendar.
 *
 * Three inputs above A8's reused picker — subject, executor persona (explicit + required,
 * A10-D-3), cadence (recurring or once, A10-D-5) — then build → PREVIEW (the engine's full
 * "When:" clause + next fire + quiet-hours warn, T2) → confirm → the T1 create door. Not a
 * form wall (the Todoist quick-add line, A10-R-2). The quiet-hours offer is actionable:
 * one tap re-times to the nearest edge and re-previews; confirming anyway is the override
 * (warn, never block, never silently shift — criterion 7). The idempotency key is minted
 * once per dialog-open (A10-D-6): double-clicks converge, two deliberate opens stay distinct.
 */

import { useMemo, useState } from "react";

import { useAuth } from "@/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  createSchedule,
  previewCreate,
  type ReschedulePreview,
} from "@/lib/api/schedule-client";
import { type CadenceInput, RecurrenceBuilder } from "./recurrence-builder";

export interface ReminderPersona {
  id: string;
  name: string;
}

export interface CreateReminderDialogProps {
  personas: ReminderPersona[];
  defaultTimezone: string;
  onClose: () => void;
  onCreated: () => Promise<void>;
}

/** Re-time a cadence to a quiet-hours edge ("HH:MM") — the accept-the-edge action. */
export function applyQuietEdge(
  cadence: CadenceInput,
  edge: string,
): CadenceInput {
  const [h, m] = edge.split(":").map((s) => Number.parseInt(s, 10));
  if (cadence.pattern) {
    return {
      pattern: { ...cadence.pattern, hour: h, minute: m },
      one_time_at: null,
    };
  }
  if (cadence.one_time_at) {
    const at = new Date(cadence.one_time_at);
    at.setHours(h, m, 0, 0); // the edge is a local wall-clock time
    return { pattern: null, one_time_at: at.toISOString() };
  }
  return cadence;
}

export function CreateReminderDialog({
  personas,
  defaultTimezone,
  onClose,
  onCreated,
}: CreateReminderDialogProps) {
  const { getToken } = useAuth();
  const [subject, setSubject] = useState("");
  const [personaId, setPersonaId] = useState("");
  // Default ON: it's a reminder — you want reminding. Unchecking keeps the schedule but
  // silences its bell (the fire still runs; it just doesn't ping).
  const [notifyOnFire, setNotifyOnFire] = useState(true);
  const [cadence, setCadence] = useState<CadenceInput | null>(null);
  const [preview, setPreview] = useState<ReschedulePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // One key per dialog-open (A10-D-6): a double-click/retry converges on ONE schedule;
  // deliberately creating the same reminder twice = two opens = two keys.
  const idempotencyKey = useMemo(() => crypto.randomUUID(), []);

  const ready =
    subject.trim().length > 0 && personaId !== "" && cadence !== null;

  async function runPreview(next: CadenceInput | null = cadence) {
    if (!next) return;
    setBusy(true);
    setError(null);
    try {
      setPreview(
        await previewCreate(await getToken(), {
          pattern: next.pattern,
          one_time_at: next.one_time_at,
          timezone: defaultTimezone,
        }),
      );
    } catch {
      setError("Couldn't preview this reminder. Check the time and try again.");
    } finally {
      setBusy(false);
    }
  }

  function acceptEdge(edge: string) {
    if (!cadence) return;
    const retimed = applyQuietEdge(cadence, edge);
    setCadence(retimed);
    void runPreview(retimed); // re-preview at the edge — the user confirms the new truth
  }

  async function doCreate() {
    if (!cadence || !ready) return;
    setBusy(true);
    setError(null);
    try {
      await createSchedule(await getToken(), {
        pattern: cadence.pattern,
        one_time_at: cadence.one_time_at,
        timezone: defaultTimezone,
        persona_id: personaId,
        subject: subject.trim(),
        idempotency_key: idempotencyKey,
        notify_on_fire: notifyOnFire,
      });
      await onCreated();
    } catch {
      setError("Couldn't create the reminder. Please try again.");
      setBusy(false);
    }
  }

  return (
    <div
      className="v-create-reminder"
      role="dialog"
      aria-label="New routine"
      data-testid="create-reminder-dialog"
    >
      <h2 className="v-dialog-title">New routine</h2>
      <p className="v-dialog-sub">
        Tell a persona what to do, and when. You'll preview before it's set.
      </p>

      <label htmlFor="reminder-subject">
        What should I do for you?
        <Input
          id="reminder-subject"
          value={subject}
          maxLength={500}
          placeholder="e.g. stretch for five minutes"
          onChange={(e) => {
            setSubject(e.target.value);
            setPreview(null);
          }}
        />
      </label>

      <label htmlFor="reminder-persona">
        Who should run it?
        <select
          id="reminder-persona"
          value={personaId}
          onChange={(e) => {
            setPersonaId(e.target.value);
          }}
        >
          <option value="" disabled>
            Choose a persona…
          </option>
          {personas.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>

      <RecurrenceBuilder
        timezone={defaultTimezone}
        onChange={(next) => {
          setCadence(next);
          setPreview(null); // a changed cadence invalidates the shown echo — re-preview
        }}
      />

      {/* Default ON — it's a reminder, so ping the bell when it runs; unset to keep it quiet. */}
      <label className="v-create-notify" htmlFor="reminder-notify">
        <input
          id="reminder-notify"
          type="checkbox"
          checked={notifyOnFire}
          onChange={(e) => {
            setNotifyOnFire(e.target.checked);
          }}
        />
        Notify me in the bell when it runs
      </label>

      {/* The confirm echo — the SAME engine-framed clause the reschedule twin shows. */}
      {preview && (
        <p className="v-create-preview" data-testid="create-preview">
          <b>When:</b> {preview.human_terms} · {preview.timezone}
          {preview.next_fire &&
            ` — next run ${new Intl.DateTimeFormat(undefined, {
              timeZone: defaultTimezone,
              weekday: "short",
              day: "numeric",
              month: "short",
              hour: "2-digit",
              minute: "2-digit",
            }).format(new Date(preview.next_fire))}`}
        </p>
      )}
      {preview?.quiet_hours_offer && (
        <p className="v-create-quiet" data-testid="quiet-offer">
          That&apos;s inside your quiet hours.
          <Button
            type="button"
            variant="outline"
            onClick={() => acceptEdge(preview.quiet_hours_offer as string)}
          >
            Move to {preview.quiet_hours_offer}
          </Button>
          — or confirm to keep your time.
        </p>
      )}
      {error && <p className="v-create-error">{error}</p>}

      <div className="v-create-actions">
        <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        {preview ? (
          <Button type="button" onClick={doCreate} disabled={busy || !ready}>
            Confirm
          </Button>
        ) : (
          <Button
            type="button"
            onClick={() => runPreview()}
            disabled={busy || !ready}
          >
            Preview
          </Button>
        )}
      </div>
    </div>
  );
}
