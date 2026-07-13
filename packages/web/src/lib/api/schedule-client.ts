/**
 * Spec A8 (T8/T9) — the calendar's typed API access.
 *
 * Hand-typed against the A8 endpoints (the generated `schema.ts` regenerates at merge-back). The
 * client sends **picker-state, never a raw RRULE** (bar 1): `RecurrencePatternInput` is the humane
 * vocabulary, mapped to the rule SERVER-side. Reschedules go through the ONE door via
 * `applyReschedule` — the same CAS-guarded path chat uses (bar 4), no second write path.
 */

import type { OccurrencesResult } from "@/lib/schedule/agenda";

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** The humane recurrence vocabulary the picker builds — no raw RRULE (mirrors core RecurrencePattern). */
export interface RecurrencePatternInput {
  kind:
    | "daily"
    | "weekly"
    | "monthly_day"
    | "monthly_weekday"
    | "hourly"
    | "yearly";
  interval?: number;
  weekdays?: string[];
  month_day?: number | null;
  weekday?: string | null;
  ordinal?: number | null;
  month?: number | null;
  day_of_month?: number | null;
  hour?: number | null;
  minute?: number;
  count?: number | null;
  until?: string | null;
}

export interface RescheduleBody {
  pattern?: RecurrencePatternInput | null;
  one_time_at?: string | null;
  timezone: string;
}

export interface ReschedulePreview {
  human_terms: string;
  timezone: string;
  next_fire: string | null;
  quiet_hours_offer: string | null;
}

/** The structured `{error, detail, context?}` shape every persona-api handler returns. */
interface ScheduleErrorBody {
  error?: string;
  detail?: string;
  context?: Record<string, string>;
}

/**
 * A failed schedule-API call — carries the HTTP status + (when the body parsed as the
 * project's structured error shape) the machine-readable `code` (R9-024: lets a caller
 * distinguish a 409 `schedule_state_conflict` — R9-023's fired-one-time re-arm guard —
 * from any other failure, instead of pattern-matching a message string).
 */
export class ScheduleApiError extends Error {
  readonly status: number;
  readonly code: string | undefined;

  constructor(
    status: number,
    path: string,
    body: ScheduleErrorBody | undefined,
  ) {
    super(body?.detail ?? `request to ${path} failed: ${status}`);
    this.name = "ScheduleApiError";
    this.status = status;
    this.code = body?.error;
  }
}

async function parseErrorBody(
  res: Response,
): Promise<ScheduleErrorBody | undefined> {
  try {
    return (await res.json()) as ScheduleErrorBody;
  } catch {
    return undefined; // a non-JSON failure body (e.g. a proxy/5xx page) — status alone still surfaces
  }
}

async function authFetch<T>(
  path: string,
  token: string | null | undefined,
  init?: RequestInit,
): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${BASE_URL}${path}`, { ...init, headers });
  if (!res.ok) {
    throw new ScheduleApiError(res.status, path, await parseErrorBody(res));
  }
  return (await res.json()) as T;
}

/** Like {@link authFetch}, for a 204-No-Content verb (delete) — never parses a JSON body. */
async function authFetchVoid(
  path: string,
  token: string | null | undefined,
  init?: RequestInit,
): Promise<void> {
  const headers = new Headers(init?.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${BASE_URL}${path}`, { ...init, headers });
  if (!res.ok) {
    throw new ScheduleApiError(res.status, path, await parseErrorBody(res));
  }
}

/** The owner's occurrences + fire history in a window — the engine's own computation (criterion 5).
 *
 * `personaId` (R9-024, optional): the chat right-panel calendar's server-side filter — omitted,
 * this is byte-identical to the `/schedule` page's unfiltered call.
 */
export function fetchOccurrences(
  token: string | null | undefined,
  from: Date,
  to: Date,
  personaId?: string,
): Promise<OccurrencesResult> {
  const q = new URLSearchParams({
    from: from.toISOString(),
    to: to.toISOString(),
  });
  if (personaId) q.set("persona_id", personaId);
  return authFetch<OccurrencesResult>(
    `/v1/me/schedule/occurrences?${q.toString()}`,
    token,
  );
}

/** Delete a schedule (R9-024) — the calendar's delete affordance, chat + `/schedule` alike. */
export function deleteSchedule(
  token: string | null | undefined,
  scheduleId: string,
): Promise<void> {
  return authFetchVoid(
    `/v1/me/schedule/${encodeURIComponent(scheduleId)}`,
    token,
    {
      method: "DELETE",
    },
  );
}

/** Preview a reschedule — the ENGINE's next-fire + full clause + quiet-hours warn (no write). */
export function previewReschedule(
  token: string | null | undefined,
  scheduleId: string,
  body: RescheduleBody,
): Promise<ReschedulePreview> {
  return authFetch<ReschedulePreview>(
    `/v1/me/schedule/${encodeURIComponent(scheduleId)}/reschedule/preview`,
    token,
    { method: "POST", body: JSON.stringify(body) },
  );
}

/** Apply a reschedule through the SAME CAS door as chat (`actor=user_via_ui`). */
export function applyReschedule(
  token: string | null | undefined,
  scheduleId: string,
  body: RescheduleBody,
): Promise<ReschedulePreview> {
  return authFetch<ReschedulePreview>(
    `/v1/me/schedule/${encodeURIComponent(scheduleId)}/reschedule`,
    token,
    { method: "POST", body: JSON.stringify(body) },
  );
}

/** A user-initiated schedule create (Spec A10, A10-D-1) — the reschedule envelope + create fields. */
export interface ScheduleCreateBody extends RescheduleBody {
  persona_id: string;
  subject: string;
  /** Minted once per dialog-open (A10-D-6): retries converge, deliberate submits stay distinct. */
  idempotency_key: string;
  /** Opt into the coalesced fire bell (server default true; the dialog checkbox is default-on). */
  notify_on_fire: boolean;
  /** R11-B2: "reminder" (default) composes the remind-and-update contract; "task"
   * schedules the subject AS the goal verbatim (the Activity dialog's Schedule-for-later). */
  intent?: "reminder" | "task";
}

/** The create confirmation — ids + the same echo shape the preview showed. */
export interface ScheduleCreateResult {
  task_id: string;
  schedule_id: string;
  created: boolean;
  human_terms: string;
  timezone: string;
  next_fire: string | null;
  quiet_hours_offer: string | null;
}

/** Preview a CREATE — the same engine preview as the reschedule twin (no write; Spec A10 T2). */
export function previewCreate(
  token: string | null | undefined,
  body: RescheduleBody,
): Promise<ReschedulePreview> {
  return authFetch<ReschedulePreview>("/v1/me/schedule/preview", token, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Create a schedule + backing task through the ONE door (`ScheduleStore`, Spec A10 T1). */
export function createSchedule(
  token: string | null | undefined,
  body: ScheduleCreateBody,
): Promise<ScheduleCreateResult> {
  return authFetch<ScheduleCreateResult>("/v1/me/schedule", token, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
