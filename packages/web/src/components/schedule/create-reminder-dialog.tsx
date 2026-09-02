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

import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { useAuth } from "@/auth";
import { ExecutorPicker } from "@/components/persona/executor-picker";
import { PersonaAvatar } from "@/components/persona/persona-avatar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  createSchedule,
  previewCreate,
  type ReschedulePreview,
} from "@/lib/api/schedule-client";
import { personaIdentityStyle } from "@/lib/persona-identity";
import { type CadenceInput, RecurrenceBuilder } from "./recurrence-builder";

export interface ReminderPersona {
  id: string;
  name: string;
  /** Real avatar for the shared executor picker (R11-B3). */
  avatar_url?: string | null;
}

export interface CreateReminderDialogProps {
  personas: ReminderPersona[];
  /** R11-B3 rider (the chat panel): the executor is THIS persona — no picker,
   * a fixed identity row instead (the kit's pre-locked executor). */
  lockedPersona?: ReminderPersona;
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
  lockedPersona,
  defaultTimezone,
  onClose,
  onCreated,
}: CreateReminderDialogProps) {
  const t = useTranslations("schedule.create");
  const tCommon = useTranslations("schedule.common");
  const { getToken } = useAuth();
  const [subject, setSubject] = useState("");
  const [personaId, setPersonaId] = useState(lockedPersona?.id ?? "");
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
      setError(t("previewFailed"));
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
      setError(t("createFailed"));
      setBusy(false);
    }
  }

  return (
    <div
      className="v-create-reminder"
      role="dialog"
      aria-label={t("title")}
      data-testid="create-reminder-dialog"
    >
      <h2 className="v-dialog-title">{t("title")}</h2>
      <p className="v-dialog-sub">{t("intro")}</p>

      <label htmlFor="reminder-subject">
        {t("subjectLabel")}
        <Input
          id="reminder-subject"
          value={subject}
          maxLength={500}
          placeholder={t("subjectPlaceholder")}
          onChange={(e) => {
            setSubject(e.target.value);
            setPreview(null);
          }}
        />
      </label>

      {/* R11-B3 (owner-ruled): the SHARED persona picker — same control as new
          chat / new call — never a bare select of name strings. In the chat
          panel the executor is LOCKED to the panel persona (kit): a fixed
          identity row, no picker. */}
      <div className="flex flex-col gap-2">
        <span className="text-sm font-medium">{t("whoLabel")}</span>
        {lockedPersona ? (
          <div
            className="flex h-10 items-center gap-2 rounded-md border border-border bg-muted/40 px-3 text-sm"
            data-slot="locked-executor"
            style={personaIdentityStyle(lockedPersona)}
          >
            <PersonaAvatar persona={lockedPersona} size="sm" />
            <span className="truncate font-medium">{lockedPersona.name}</span>
          </div>
        ) : (
          <ExecutorPicker
            personas={personas}
            value={personaId}
            onSelect={setPersonaId}
            label={t("whoLabel")}
            placeholder={t("whoPlaceholder")}
          />
        )}
      </div>

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
        {t("notify")}
      </label>

      {/* The confirm echo — the SAME engine-framed clause the reschedule twin shows. */}
      {preview && (
        <p className="v-create-preview" data-testid="create-preview">
          <b>{tCommon("whenLabel")}</b> {preview.human_terms} ·{" "}
          {preview.timezone}
          {preview.next_fire &&
            `, ${tCommon("nextRun", {
              when: new Intl.DateTimeFormat(undefined, {
                timeZone: defaultTimezone,
                weekday: "short",
                day: "numeric",
                month: "short",
                hour: "2-digit",
                minute: "2-digit",
              }).format(new Date(preview.next_fire)),
            })}`}
        </p>
      )}
      {preview?.quiet_hours_offer && (
        <p className="v-create-quiet" data-testid="quiet-offer">
          {t("quietTitle")}
          <Button
            type="button"
            variant="outline"
            onClick={() => acceptEdge(preview.quiet_hours_offer as string)}
          >
            {t("quietMove", { edge: preview.quiet_hours_offer })}
          </Button>
          {t("quietOr")}
        </p>
      )}
      {error && <p className="v-create-error">{error}</p>}

      <div className="v-create-actions">
        <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>
          {tCommon("cancel")}
        </Button>
        {preview ? (
          <Button type="button" onClick={doCreate} disabled={busy || !ready}>
            {tCommon("confirm")}
          </Button>
        ) : (
          <Button
            type="button"
            onClick={() => runPreview()}
            disabled={busy || !ready}
          >
            {tCommon("preview")}
          </Button>
        )}
      </div>
    </div>
  );
}
