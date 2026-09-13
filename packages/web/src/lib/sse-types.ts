/**
 * Hand-mirrored SSE event shapes (D-09-1). OpenAPI cannot model SSE streams, so
 * these are mirrored BY HAND from the frozen Pydantic models on the API side.
 * Keep in sync with:
 *   - chat events:  packages/api/src/persona_api/services/chat_service.py (_sse)
 *   - run events:   packages/runtime/src/persona_runtime/agentic/events.py (RunEvent)
 *
 * ⚠ The two streams use DIFFERENT envelopes (research.md §2.3 — the #1 silent-bug
 * risk):
 *   - CHAT frames serialise the BARE payload: `data:` IS the payload.
 *       e.g.  event: tool_result\ndata: {"tool_name","is_error","content"}
 *   - RUN frames serialise the WHOLE RunEvent: `data:` is
 *       {type, step, data, timestamp} — the payload is nested under `.data`.
 *       e.g.  event: tool_result\ndata: {"type":"tool_result","step":0,
 *               "data":{"tool_name","is_error","content"},"timestamp":"..."}
 *
 * Both streams share the SAME tool-event vocabulary (tool_calling / tool_result)
 * after the spec-08 chat-SSE patch (D-09-12).
 */

import type { components } from "./api/schema";
import type { RawSSEEvent } from "./sse";

/**
 * Spec 30 (D-30-1) — the source badge the runtime resolves for every tool call
 * (`persona.tools.kind.resolve_tool_kind`). Mirrors the four-value Python
 * taxonomy. `string` (not a strict union) on the wire types because the field
 * is additive + forward-compatible; the card narrows it for rendering.
 */
export type ToolKind = "builtin" | "skill" | "mcp:builtin" | "mcp:optional";

// ----- shared tool-event payloads (identical in chat + run) -----

export interface ToolCallPayload {
  name: string;
  call_id: string;
  args: Record<string, unknown>;
  /**
   * Spec 30 T01 (D-30-1): the call's source badge. Additive — absent on
   * pre-spec-30 frames (the `options`/`produced_files` precedent); the chat +
   * run consumers fall back to an unbadged card when absent.
   */
  kind?: string;
}

export interface ToolCallingData {
  tool_names: string;
  tool_calls: ToolCallPayload[];
}

/**
 * P2 — the pre-execution "using <X>…" signal. Emitted before the persona invokes
 * any capability (builtin tool / skill / MCP / sandbox / imagegen / web), paired with
 * an {@link ActivityEndData} by `activity_id`. Drives the live "using <X>…" state.
 * `args_summary` is redacted at the core emit boundary — never carries secrets.
 *
 * Coexists with `tool_calling`/`tool_result` during the migration (P2-D-3 keep-both):
 * the activity events drive the live state (a SEPARATE channel from the tool card),
 * so a call renders ONE card (from `tool_result`) plus a transient activity state —
 * not two cards. `tool_result` stays the card source until its retirement.
 */
export interface ActivityStartData {
  activity_id: string;
  /** `tool` / `skill` / `mcp` / `sandbox` / `imagegen` / `web` / `memory_recall`. */
  kind: string;
  /** The specific name (tool name, `mcp:server:tool`, `use_skill`). */
  name: string;
  /** Human label for the affordance ("Searching the web"). */
  label: string;
  /** Redacted, bounded arg summary (never secrets). */
  args_summary: Record<string, string>;
}

/**
 * P2 — the post-execution half, paired to its {@link ActivityStartData} by
 * `activity_id`. `status` resolves/clears (or error/awaiting-approval marks) the live
 * state. Lightweight by design (P2-D-3): the rich-output payload (content /
 * produced_files / artifacts) stays on the coexisting `tool_result` during keep-both.
 */
export interface ActivityEndData {
  activity_id: string;
  /** `ok` / `error` / `denied` / `awaiting_approval`. */
  status: string;
  duration_ms: number;
  is_error: boolean;
  result_summary?: string;
}

/**
 * One produced file in a `tool_result` event's structured payload.
 *
 * Wire-shape mirror of the runtime forwarding amendment at
 * `packages/runtime/src/persona_runtime/agentic/events.py:96-103`
 * (D-F4-X-event-kind-for-produced-files, Spec F4 T02b). The runtime
 * forwards `ToolResult.data["produced_files"]` onto the event payload
 * verbatim; this interface mirrors the dict shape the sandbox tool
 * factory populates at
 * `packages/core/src/persona/sandbox/tool.py:269-279`.
 *
 * Additive — absent on pre-amendment frames and on tools that don't
 * produce files (web_search, file_*, etc.). The F4 chat + run normalisers
 * read this when present; absence falls back to a `result-block` render.
 */
export interface ProducedFileRef {
  path: string;
  size_bytes: number;
  media_type?: string | null;
}

/**
 * Spec 28 — a persisted tool artifact forwarded from `ToolResult.artifacts`
 * (`PersistedArtifact.model_dump()`). When present, the F4 normaliser renders
 * each as a `file-card` (inline thumbnail/SVG + right-panel renderer), in
 * preference to the legacy `produced_files` path. Absent on pre-Spec-28 frames
 * and on tools that persist nothing.
 */
export interface ArtifactRef {
  workspace_path: string;
  mime_type: string;
  size_bytes: number;
  rendered_inline: boolean;
}

export interface ToolResultData {
  tool_name: string;
  is_error: boolean;
  content: string;
  /**
   * Spec 30 T01 (D-30-1): the call's source badge (see {@link ToolCallPayload.kind}).
   * Additive — absent on pre-spec-30 frames.
   */
  kind?: string;
  /**
   * D-F4-X-event-kind-for-produced-files: structured produced files
   * surfaced from sandbox-backed tools (Spec 12 stdout-only / Spec 16
   * docx-pptx-xlsx-pdf / Spec 17 charts). Omitted on tools that don't
   * produce files and on pre-T02b frames (back-compat: F4 normalisers
   * fall back to a result-block render when absent).
   */
  produced_files?: ProducedFileRef[];
  /**
   * Spec 28 — persisted byte-outputs (image / file / diagram). Preferred over
   * `produced_files` when present (the unified FileCard render path). Omitted
   * on pre-Spec-28 frames and tools that persist nothing.
   */
  artifacts?: ArtifactRef[];
}

// =================== CHAT stream (bare-payload frames) ===================

export interface ChatChunkData {
  delta: string;
  is_final: boolean;
}

/**
 * Spec 35 (D-35-4) — the chat "thinking / remembering" state. One frame per
 * typed-memory store consulted while composing, naming the store + how many
 * chunks it contributed this turn. The chat stages a store-coloured
 * "Recalling from <store> memory" pulse from these; the run stream carries the
 * same vocabulary. Shared by the chat and run transports.
 */
export interface MemoryRecallData {
  store: "identity" | "self_facts" | "worldview" | "episodic";
  count?: number;
}

/**
 * Spec 31 (D-31-1) — the concise model-decision summary on the `done` event.
 * Mirrors the API `RoutingSummary` (responses.py). Structured/enum fields only:
 * the web templates the localized "why" phrase from `dominant_factor` +
 * `chosen_model`; the raw score vector stays in the audit JSONL, never here.
 * Present only on intelligent-routing turns (absent ⇒ bare tier badge).
 */
export interface RoutingSummary {
  chosen_model: string;
  dominant_factor: "cost" | "quality" | "latency" | null;
  model_fallback_engaged: boolean;
  model_fallback_reason: string | null;
}

/**
 * Spec 31 (D-31-2) — per-session budget snapshot on the `done` event. Mirrors
 * the API `BudgetSnapshot`. `session_spent_cents` includes the just-completed
 * turn; caps are omitted when unset. Present only when intelligent routing is
 * on and a cap is configured.
 */
export interface BudgetSnapshot {
  session_spent_cents: number;
  max_cents_per_turn?: number;
  max_cents_per_session?: number;
  max_cents_per_day?: number;
}

export interface ChatDoneData {
  // {} when usage is unavailable; otherwise the token counts.
  usage: { prompt_tokens?: number; completion_tokens?: number };
  tier: string;
  format_hints: Record<string, string>;
  /** Spec 31 — SEPARATE additive routing (D-31-1) + budget (D-31-2) fields. */
  routing?: RoutingSummary;
  budget?: BudgetSnapshot;
}

/** A parsed chat SSE event, discriminated by the `event:` name. */
export type ChatEvent =
  | { event: "chunk"; data: ChatChunkData }
  | { event: "tool_calling"; data: ToolCallingData }
  | { event: "tool_result"; data: ToolResultData }
  // P2: the unified "using <X>…" activity contract (start/end paired by activity_id).
  | { event: "activity_start"; data: ActivityStartData }
  | { event: "activity_end"; data: ActivityEndData }
  // Spec 30 (D-30-2): the chat-proactive-question rail. The shared loop already
  // emits `asking_user` to the chat stream (tool-gap / MCP-gap offers); the web
  // now parses it (previously dropped) so the rail can render inline.
  | { event: "asking_user"; data: AskingUserData }
  // Spec 35 (D-35-4): the typed-memory recall state, named per store.
  | { event: "memory_recall"; data: MemoryRecallData }
  // The model is generating this round — drives a "working" pulse during the
  // gap before any text/tool event (notably while writing a long code_execution
  // call, whose args don't stream as deltas). Empty payload; cleared on the
  // next chunk/tool_calling/tool_result.
  | { event: "thinking"; data: Record<string, never> }
  | { event: "done"; data: ChatDoneData };

const CHAT_EVENTS = new Set([
  "chunk",
  "tool_calling",
  "tool_result",
  "activity_start",
  "activity_end",
  "asking_user",
  "memory_recall",
  "thinking",
  "done",
  // PENDING-web seam (Spec C0, D-C0-X-within-runtime-default-off): the api can
  // push a "persona_originated" event (a persona-initiated message) inline on the
  // open stream when PERSONA_API_WITHIN_RUNTIME_ORIGINATION is enabled. C0 (Layer
  // 1+3) does not render it; enabling the feature pairs with a Layer-4 follow-up
  // here — add "persona_originated" to CHAT_EVENTS + a render case in use-chat,
  // and an "originated" badge on the assistant bubble. Until then the message is
  // never lost: it is a persisted assistant message, rendered present-on-next-open.
]);

/**
 * Warn (dev only) when the api emits an SSE event the web does not handle, so a
 * missed live-render is loud, not silent. The durable render (persisted message on
 * next open) is the backstop, so this is a missed-live-render, not data loss.
 */
export function warnUnhandledSseEvent(stream: string, eventName: string): void {
  if (process.env.NODE_ENV !== "production") {
    // Intentional dev-only trace so a missed live-render is loud, not silent.
    console.warn(
      `[sse] unhandled ${stream} event "${eventName}" — dropped (no Layer-4 handler). ` +
        `If this is "persona_originated", see the Spec C0 PENDING-web seam.`,
    );
  }
}

/**
 * Parse one raw chat frame into a typed {@link ChatEvent}. `data` is the bare
 * payload. Returns null for unknown event names (forward-compatible). The wire
 * is our own trusted API, so we cast after a name check rather than deep-validate.
 */
export function parseChatEvent(raw: RawSSEEvent): ChatEvent | null {
  if (!CHAT_EVENTS.has(raw.event)) {
    warnUnhandledSseEvent("chat", raw.event);
    return null;
  }
  const data = JSON.parse(raw.data) as unknown;
  return { event: raw.event, data } as ChatEvent;
}

// =============== AUTHORING stream (bare-payload frames, spec P0) ===============
//
// Mirrors chat's vocabulary exactly (D-P0-event-vocab): `chunk` carries the same
// `{delta, is_final}` as chat; `retry` is a visible regenerating signal when the
// validation-repair re-stream starts; `draft` is the single TERMINAL event
// carrying the full validated-or-errored `AuthoringDraft` (D-P0-errors-on-terminal,
// the preserved codegen type); `done` is chat's closing sentinel. Like chat, the
// frames serialise the BARE payload (`data:` IS the payload).

/** The terminal authoring payload — the preserved OpenAPI type (R-P0-2). */
export type AuthoringDraftData = components["schemas"]["AuthoringDraft"];

export interface AuthorRetryData {
  /** Why the stream is regenerating (currently always `"validation"`). */
  reason: string;
}

/** A parsed authoring SSE event, discriminated by the `event:` name. */
export type AuthorEvent =
  | { event: "chunk"; data: ChatChunkData }
  | { event: "retry"; data: AuthorRetryData }
  | { event: "draft"; data: AuthoringDraftData }
  | { event: "done"; data: Record<string, never> };

const AUTHOR_EVENTS = new Set(["chunk", "retry", "draft", "done"]);

/**
 * Parse one raw authoring frame into a typed {@link AuthorEvent}. `data` is the
 * bare payload (same envelope as chat). Returns null for unknown event names
 * (forward-compatible). The wire is our own trusted API, so we cast after a name
 * check rather than deep-validate (mirrors {@link parseChatEvent}).
 */
export function parseAuthorEvent(raw: RawSSEEvent): AuthorEvent | null {
  if (!AUTHOR_EVENTS.has(raw.event)) return null;
  const data = JSON.parse(raw.data) as unknown;
  return { event: raw.event, data } as AuthorEvent;
}

// =================== RUN stream (RunEvent envelope frames) ===================

export type RunEventType =
  | "started"
  | "tier"
  | "thinking"
  | "memory_recall"
  | "tool_calling"
  | "tool_result"
  | "activity_start"
  | "activity_end"
  | "asking_user"
  | "user_responded"
  | "reasoning"
  | "call_skipped"
  | "context_pruned"
  | "completed"
  | "cancelled"
  | "max_steps"
  | "error"
  | "finished";
// PENDING-web seam (Spec C0): a "persona_originated" run event (a persona-initiated
// message inline on the run stream) is NOT in this union — C0 (Layer 1+3) emits it
// (api) but does not render it (Layer 4). Enabling PERSONA_API_WITHIN_RUNTIME_ORIGINATION
// pairs with adding "persona_originated" here + a case in run.ts `runViewFromEvents`
// + an "originated" badge. Until then it is dropped (loudly — see run.ts default),
// and the message is never lost (persisted, rendered present-on-next-open).

interface RunEventBase {
  step: number;
  timestamp: string;
}

type EmptyData = Record<string, never>;

export interface StartedData {
  task: string;
}
export interface TierData {
  tier: string;
}
export interface ReasoningData {
  content: string;
}
/**
 * Spec W1 (D-W1-11): a tool call the run answered from its own ledger instead of
 * dispatching. `guard` is `"repeat_error"` (this exact call already failed this run) or
 * `"cached_read"` (this exact read already succeeded and nothing has changed since).
 */
export interface CallSkippedData {
  tool: string;
  guard: string;
}
/**
 * Spec W1 (D-W1-13): older tool output was trimmed to a bounded head once the step's
 * context passed the cost ceiling. The token counts are what the step cost before and
 * after, so the trace shows the saving rather than the model quietly losing detail.
 */
export interface ContextPrunedData {
  before_tokens: number;
  after_tokens: number;
}
/** One predefined answer option for a proactive question (spec 21 T04, D-21-9). */
export interface QuestionOption {
  label: string;
  description?: string;
}
/**
 * Spec 30 (D-30-2) — the general, source-agnostic action a proactive question
 * proposes. The LOCKED `{kind, name, provider?, action}` envelope the chat rail
 * carries (tool-gap + MCP-gap now; Spec 31 autonomy prompts later). The web maps
 * `action` → the endpoint to call on accept.
 */
export interface ProactiveProposal {
  /** Category: `"tool"` / `"mcp"` (spec 30); future sources set their own. */
  kind: string;
  /** The identifier the action consumes — a tool name or `mcp:<server>` entry. */
  name: string;
  /** What to do on accept: `"grant_tool"` (POST /personas/{id}/tools) / `"assign_mcp"` / … */
  action: string;
  /** Provider tag for display — `"builtin"` / `"mcp:builtin"` / `"mcp:optional"`. */
  provider?: string;
}

export interface AskingUserData {
  question: string;
  /**
   * Spec 21 (D-21-9): the 3+1 proactive-question options. Additive — absent on
   * the pre-spec-21 / model-initiated free-text ask, present (exactly 3) when
   * the persona offers predefined options. The renderer shows option buttons +
   * a free-form field when present, and the plain free-text field when absent.
   */
  options?: QuestionOption[];
  allow_free_form?: boolean;
  /**
   * Spec 30 (D-30-2): the source-agnostic accept→grant/assign→retry descriptor.
   * Absent on a plain clarifying ask; present on a capability-gap offer.
   */
  proposal?: ProactiveProposal;
}
export interface CompletedData {
  output: string;
}
export interface MaxStepsData {
  summary: string;
}
export interface RunErrorData {
  message: string;
}
export interface FinishedData {
  run_id: string;
  status: string;
}

/** A parsed run SSE event — the full RunEvent envelope, discriminated by `type`. */
export type RunEvent =
  | (RunEventBase & { type: "started"; data: StartedData })
  | (RunEventBase & { type: "tier"; data: TierData })
  | (RunEventBase & { type: "thinking"; data: EmptyData })
  | (RunEventBase & { type: "memory_recall"; data: MemoryRecallData })
  | (RunEventBase & { type: "tool_calling"; data: ToolCallingData })
  | (RunEventBase & { type: "tool_result"; data: ToolResultData })
  | (RunEventBase & { type: "activity_start"; data: ActivityStartData })
  | (RunEventBase & { type: "activity_end"; data: ActivityEndData })
  | (RunEventBase & { type: "asking_user"; data: AskingUserData })
  | (RunEventBase & { type: "user_responded"; data: EmptyData })
  | (RunEventBase & { type: "reasoning"; data: ReasoningData })
  | (RunEventBase & { type: "call_skipped"; data: CallSkippedData })
  | (RunEventBase & { type: "context_pruned"; data: ContextPrunedData })
  | (RunEventBase & { type: "completed"; data: CompletedData })
  | (RunEventBase & { type: "cancelled"; data: EmptyData })
  | (RunEventBase & { type: "max_steps"; data: MaxStepsData })
  | (RunEventBase & { type: "error"; data: RunErrorData })
  | (RunEventBase & { type: "finished"; data: FinishedData });

/** The terminal frame the run stream emits after `finished` (`event: end`). */
export const SSE_RUN_END_EVENT = "end";

/**
 * Parse one raw run frame into a typed {@link RunEvent}. `data` is the full
 * RunEvent envelope (payload nested under `.data`). Returns null for the
 * terminal `end` frame and for malformed frames; the caller stops on `end`.
 */
export function parseRunEvent(raw: RawSSEEvent): RunEvent | null {
  if (raw.event === SSE_RUN_END_EVENT) return null;
  const env = JSON.parse(raw.data) as { type?: unknown };
  if (typeof env.type !== "string") return null;
  return env as RunEvent;
}
