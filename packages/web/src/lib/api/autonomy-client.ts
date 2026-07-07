/**
 * Spec A6 (W7) — the autonomy kill-switches' typed API access.
 *
 * Hand-typed against the B4 autonomy-controls endpoints (the generated `schema.ts` regenerates at
 * merge-back — the W4/A8 precedent). Every switch reflects the DURABLE presence state (fetched,
 * never assumed) and is idempotent — a double-press is a calm no-op (`changed: false`), never an
 * error. HONESTY: until the owner-pause predicate is injected into A7/A10/A5 origination at
 * merge-back, these switches stop the task-leg path but not yet those origination sources — the UI
 * copy says only what is true now (see the `autonomyControls` messages).
 */

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** The owner's autonomy-pause state — the durable presence read (B4). */
export interface AutonomyState {
  paused: boolean;
  changed: boolean;
  note: string;
}

/** A single persona's autonomy-suspension state — presence-based (B4). */
export interface PersonaSuspension {
  persona_id: string;
  suspended: boolean;
  changed: boolean;
  note: string;
}

async function authFetch<T>(
  path: string,
  token: string | null | undefined,
  method: "GET" | "POST" = "GET",
): Promise<T> {
  const headers = new Headers({ "Content-Type": "application/json" });
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${BASE_URL}${path}`, { method, headers });
  if (!res.ok) throw new Error(`${method} ${path} failed: ${res.status}`);
  return (await res.json()) as T;
}

/** Is the caller's autonomy paused? The durable presence read, RLS-scoped. */
export const getAutonomyState = (t: string | null | undefined) =>
  authFetch<AutonomyState>("/v1/autonomy/state", t);
/** Pause ALL of the caller's autonomy (owner-wide). Idempotent calm reflection. */
export const pauseAutonomy = (t: string | null | undefined) =>
  authFetch<AutonomyState>("/v1/autonomy/pause", t, "POST");
/** Resume the caller's autonomy. Idempotent calm reflection. */
export const resumeAutonomy = (t: string | null | undefined) =>
  authFetch<AutonomyState>("/v1/autonomy/resume", t, "POST");

const personaPath = (id: string, suffix: string) =>
  `/v1/autonomy/personas/${encodeURIComponent(id)}${suffix}`;

/** Is this persona's autonomy suspended? The durable presence read. */
export const getPersonaSuspension = (
  t: string | null | undefined,
  id: string,
) => authFetch<PersonaSuspension>(personaPath(id, "/state"), t);
/** Suspend one persona's autonomy — no new legs for its tasks. Idempotent. */
export const suspendPersona = (t: string | null | undefined, id: string) =>
  authFetch<PersonaSuspension>(personaPath(id, "/suspend"), t, "POST");
/** Resume one persona's autonomy. Idempotent. */
export const resumePersona = (t: string | null | undefined, id: string) =>
  authFetch<PersonaSuspension>(personaPath(id, "/resume"), t, "POST");
