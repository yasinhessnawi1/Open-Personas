/**
 * Spec A6 (W2/W3) — the Tasks surface's typed API access.
 *
 * Hand-typed against the A6 tasks endpoints (the generated `schema.ts` regenerates at merge-back —
 * the W4/A8 precedent). Reads are RLS-scoped and faithful; commands are the B2 mutations (audited,
 * idempotent — a double-press reflects the durable truth calmly, never an error). Durable state is
 * truth on load (A6-R-4): the list/detail is fetched, never inferred.
 */

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** One task in the cross-persona list (the state matrix + spend). */
export interface TaskSummary {
  task_id: string;
  persona_id: string;
  goal: string;
  status: string; // just_created | progressing | waiting_on_user | scheduled | paused | completed | failed | cancelled
  paused: boolean;
  spent_micros: number;
  budget_cap_micros: number;
  updated_at: string;
  /** The blocked_on reason, for the waiting_on_user/failed subset only (A6-D-5); null otherwise. */
  stuck_cause: string | null;
}

/** The durable post-state of a task command — `changed` false = an idempotent no-op (B2). */
export interface TaskCommandResult {
  task_id: string;
  status: string;
  paused: boolean;
  changed: boolean;
  owner_autonomy_paused: boolean;
  note: string;
}

/** The result of a budget extension — bounded, at-most-once, with the old → new cap (B2). */
export interface BudgetExtendResult {
  task_id: string;
  applied: boolean;
  old_cap_micros: number;
  new_cap_micros: number;
  state: string; // ok | approaching | reached
  note: string;
}

/** A persona's initiative restraint level + the honest platform flag (B4). */
export interface InitiativeDialState {
  persona_id: string;
  dial: string; // off | propose_only | act_within_envelope
  changed: boolean;
  initiative_enabled: boolean;
  note: string;
}

export interface Grant {
  category: string;
  decision: string; // allow | gate | deny
}
export interface AcceptanceCriterion {
  id: string;
  statement: string;
  status: string;
}
export interface Budget {
  cap_micros: number;
  spent_micros: number;
  state: string; // ok | approaching | reached
}
export interface Ledger {
  model_micros: number;
  sandbox_micros: number;
  external_micros: number;
  total_micros: number;
}
export interface Checkpoint {
  seq: number;
  progress_conclusions: string[];
  next_step: string;
  open_questions: string[];
  blocked_on: string | null;
  updated_at: string;
}
/** The terminal outcome as its OWN projection — a failure never renders as success (A6-D-4). */
export interface TaskReport {
  kind: "completed" | "stuck" | "cancelled";
  conclusions?: string[];
  cause?: string;
  where_it_stood?: string[];
  next_step?: string;
}

/** The full task detail above the run viewer (contract + grants, budget, ledger, checkpoints). */
export interface TaskDetail {
  task_id: string;
  persona_id: string;
  goal: string;
  scope: string;
  status: string;
  paused: boolean;
  grants: Grant[];
  acceptance_criteria: AcceptanceCriterion[];
  deadline: string | null;
  max_legs: number | null;
  budget: Budget;
  ledger: Ledger;
  progress: string[];
  next_step: string;
  open_questions: string[];
  wait_reason: string | null;
  report: TaskReport | null;
  checkpoints: Checkpoint[];
  conversation_id: string | null;
  schedule_id: string | null;
  run_ids: string[];
  created_at: string;
  updated_at: string;
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
    throw new Error(`${init?.method ?? "GET"} ${path} failed: ${res.status}`);
  }
  return (await res.json()) as T;
}

/** Standing + recent tasks across the caller's personas (RLS-scoped, faithful). */
export function fetchTasks(
  token: string | null | undefined,
): Promise<TaskSummary[]> {
  return authFetch<TaskSummary[]>("/v1/tasks", token);
}

/** One task's full detail (RLS-scoped; the truth on load). */
export function getTask(
  token: string | null | undefined,
  taskId: string,
): Promise<TaskDetail> {
  return authFetch<TaskDetail>(
    `/v1/tasks/${encodeURIComponent(taskId)}`,
    token,
  );
}

function command(
  token: string | null | undefined,
  taskId: string,
  verb: "pause" | "resume" | "cancel",
): Promise<TaskCommandResult> {
  return authFetch<TaskCommandResult>(
    `/v1/tasks/${encodeURIComponent(taskId)}/${verb}`,
    token,
    { method: "POST" },
  );
}

/** Pause a task (no new legs). Idempotent — a paused task is a calm no-op. */
export const pauseTask = (t: string | null | undefined, id: string) =>
  command(t, id, "pause");
/** Resume a paused task. If owner autonomy is paused, the result reflects it (won't arm). */
export const resumeTask = (t: string | null | undefined, id: string) =>
  command(t, id, "resume");
/** Cancel a task (terminal) — a running step finishes first; idempotent on a terminal task. */
export const cancelTask = (t: string | null | undefined, id: string) =>
  command(t, id, "cancel");

/** Raise a budget-paused task's cap (bounded, at-most-once). Returns the old → new cap. */
export function extendBudget(
  token: string | null | undefined,
  taskId: string,
  amountMicros: number,
): Promise<BudgetExtendResult> {
  return authFetch<BudgetExtendResult>(
    `/v1/tasks/${encodeURIComponent(taskId)}/budget/extend`,
    token,
    { method: "POST", body: JSON.stringify({ amount_micros: amountMicros }) },
  );
}

/** This persona's durable initiative restraint level + the honest platform flag (B4). */
export function getInitiativeDial(
  token: string | null | undefined,
  personaId: string,
): Promise<InitiativeDialState> {
  return authFetch<InitiativeDialState>(
    `/v1/autonomy/personas/${encodeURIComponent(personaId)}/initiative`,
    token,
  );
}

/** Set this persona's initiative restraint level (the dial). Idempotent calm reflection. */
export function setInitiativeDial(
  token: string | null | undefined,
  personaId: string,
  dial: "off" | "propose_only" | "act_within_envelope",
): Promise<InitiativeDialState> {
  return authFetch<InitiativeDialState>(
    `/v1/autonomy/personas/${encodeURIComponent(personaId)}/initiative`,
    token,
    { method: "PUT", body: JSON.stringify({ dial }) },
  );
}
