/**
 * Spec A6 (B3/W4) — the approvals inbox's typed API access.
 *
 * Hand-typed against the A6 approvals endpoints (the generated `schema.ts` regenerates at
 * merge-back). Every decision goes through the ONE shared resolution service the chat twin uses,
 * so a chat-vs-inbox race resolves once — the durable `status` in the result is the honest
 * reflection (A6-D-3), never a cached guess. The proposal's `arguments` are the EXACT recorded
 * payload; the UI renders them verbatim as TEXT, never HTML (a safety surface, not a summary).
 */

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

/** One approval, faithfully (the exact recorded payload) plus what the record says about it. */
export interface ApprovalOut {
  proposal_id: string;
  task_id: string;
  persona_id: string;
  tool_name: string;
  arguments: Record<string, JsonValue>;
  description: string;
  categories: string[];
  created_at: string;
  expires_at: string;
  /** pending | approved | modified | denied | expired | consumed (the durable ProposalStatus). */
  status: string;
  /** True when the user changed the action before saying yes (from the durable decision trail). */
  edited: boolean;
}

export type InboxDecision = "approve" | "deny" | "modify";

export interface ApprovalDecisionRequest {
  decision: InboxDecision;
  edited_arguments?: Record<string, JsonValue> | null;
  note?: string;
}

/** The durable post-state of a decision — `status` is the reflection anchor (A6-D-3). */
export interface ApprovalDecisionResult {
  outcome: string | null; // approve|deny|modify|clarify, or null on an idempotent no-op
  executed: boolean;
  note: string;
  status: string; // the durable ProposalStatus after resolution
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

/** Every pending approval across the caller's tasks, oldest-first (RLS-scoped, faithful). */
export function fetchApprovals(
  token: string | null | undefined,
): Promise<ApprovalOut[]> {
  return authFetch<ApprovalOut[]>("/v1/approvals", token);
}

/**
 * The most recently handled approvals, newest first.
 *
 * The pending list drops a proposal the moment it is decided, so without this the sentence the
 * inbox showed after a decision lived only as long as the tab did. These are the same durable
 * rows read back, which is what lets a reopened inbox still say "Approved with your edits".
 */
export function fetchHandledApprovals(
  token: string | null | undefined,
): Promise<ApprovalOut[]> {
  return authFetch<ApprovalOut[]>("/v1/approvals/handled", token);
}

/** One approval, faithfully, whatever its status. */
export function getApproval(
  token: string | null | undefined,
  proposalId: string,
): Promise<ApprovalOut> {
  return authFetch<ApprovalOut>(
    `/v1/approvals/${encodeURIComponent(proposalId)}`,
    token,
  );
}

/** Approve / deny / modify through the shared resolver; the result's `status` is durable. */
export function decideApproval(
  token: string | null | undefined,
  proposalId: string,
  body: ApprovalDecisionRequest,
): Promise<ApprovalDecisionResult> {
  return authFetch<ApprovalDecisionResult>(
    `/v1/approvals/${encodeURIComponent(proposalId)}/decision`,
    token,
    { method: "POST", body: JSON.stringify(body) },
  );
}
