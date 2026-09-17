/**
 * Spec P3 (P3-D-3) — the ONE pure reducer that folds a parsed chat event into a
 * `ChatMessageView`, shared by the live stream and the persisted-log
 * reconstruction. This sharing IS the anti-drift guarantee (the spec's #1 risk):
 * the live `applyTurnFrame` and `persistedToView` both fold through
 * `reduceChatEvent`, so the live `ChatEvent`/`MessageEvent` union and the
 * persisted log cannot structurally fork — a divergence would break the reducer.
 *
 * - `reduceChatEvent(view, ev)` — pure `(ChatMessageView, ChatEvent) → ChatMessageView`,
 *   extracted verbatim from the old `applyTurnFrame` body (live render unchanged).
 * - `toChatEvent(entry)` — map ONE persisted `stream_events` entry to a `ChatEvent`.
 *   The persisted log is a hybrid (sse-types.ts §envelope): RunEvent dumps
 *   `{type, step, data, timestamp}` interleaved with text deltas
 *   `{kind: "text", delta}`. `RunEvent.data` IS the bare chat-SSE payload the
 *   reducer already consumes, so the map is mechanical.
 * - `persistedToView(message)` — fold a persisted message's `events[]` into the
 *   reconstructed view; absent/empty `events` → byte-exact text-only (criterion 5).
 */

import type { ChatMessageView } from "@/components/chat/message-element";
import { reduceActivityEnd, reduceActivityStart } from "@/lib/activity";
import type { ChatEvent } from "@/lib/sse-types";

/**
 * Fold one parsed chat event into the assistant turn view. Pure: returns a new
 * view, never mutates. Unknown / unhandled events return the view unchanged
 * (forward-compatible — mirrors the live path dropping an unhandled frame).
 */
export function reduceChatEvent(
  a: ChatMessageView,
  ev: ChatEvent,
): ChatMessageView {
  if (ev.event === "thinking") {
    // The model is generating this round — show a "working" pulse during the gap
    // before any text/tool event (notably while writing a long code_execution
    // call). Cleared by the next chunk / tool_calling.
    return { ...a, working: true };
  }
  if (ev.event === "chunk") {
    return {
      ...a,
      working: false,
      content: a.content + ev.data.delta,
      events: [
        ...(a.events ?? []),
        { kind: "text", delta: ev.data.delta } as const,
      ],
    };
  }
  if (ev.event === "tool_calling") {
    return {
      ...a,
      working: false,
      tools: [
        ...(a.tools ?? []),
        ...ev.data.tool_calls.map((c) => ({
          toolName: c.name,
          args: c.args,
          pending: true,
          // Spec 30 T01 (D-30-1): the source badge the card renders.
          kind: c.kind,
        })),
      ],
      events: [
        ...(a.events ?? []),
        ...ev.data.tool_calls.map(
          (c) =>
            ({
              kind: "tool_call",
              callId: c.call_id,
              toolName: c.name,
              args: c.args,
              toolKind: c.kind,
            }) as const,
        ),
      ],
    };
  }
  if (ev.event === "tool_result") {
    const tools = [...(a.tools ?? [])];
    for (let i = tools.length - 1; i >= 0; i--) {
      if (tools[i].toolName === ev.data.tool_name && tools[i].pending) {
        tools[i] = {
          ...tools[i],
          result: ev.data.content,
          isError: ev.data.is_error,
          pending: false,
          // Prefer the result frame's kind; keep the call's if absent.
          kind: ev.data.kind ?? tools[i].kind,
        };
        break;
      }
    }
    return {
      ...a,
      tools,
      events: [
        ...(a.events ?? []),
        {
          kind: "tool_result",
          toolName: ev.data.tool_name,
          content: ev.data.content,
          isError: ev.data.is_error,
          toolKind: ev.data.kind,
          // F4 T02b: forward structured produced_files when the runtime
          // amendment surfaces them. Renders inline via the OutputDispatcher in
          // MessageElement (T10). Absent on pre-amendment frames + tools that
          // don't produce files.
          producedFiles: ev.data.produced_files,
          // Spec 28: forward persisted artifacts (the unified FileCard path;
          // preferred over produced_files downstream).
          artifacts: ev.data.artifacts,
        } as const,
      ],
    };
  }
  if (ev.event === "activity_start") {
    // P2: open the live "using <X>…" state — a SEPARATE channel from `tools` (the card
    // stays sourced from tool_result during keep-both, P2-D-3). Idempotent on the
    // reattach replay / persisted reconstruction (dedup by activity_id).
    return {
      ...a,
      working: false,
      activities: reduceActivityStart(a.activities, ev.data),
    };
  }
  if (ev.event === "activity_end") {
    // P2: resolve the matching live state by activity_id (no-op if no start seen).
    return { ...a, activities: reduceActivityEnd(a.activities, ev.data) };
  }
  if (ev.event === "asking_user") {
    // Spec 30 (D-30-2): the chat-proactive-question rail.
    return {
      ...a,
      proactive: {
        question: ev.data.question,
        options: ev.data.options,
        allowFreeForm: ev.data.allow_free_form,
        proposal: ev.data.proposal,
      },
    };
  }
  if (ev.event === "memory_recall") {
    // Spec 35 (D-35-4): the "thinking / remembering" state — one frame per typed
    // store consulted while composing.
    return {
      ...a,
      recall: [
        ...(a.recall ?? []),
        { store: ev.data.store, count: ev.data.count },
      ],
    };
  }
  if (ev.event === "tier") {
    // The tier lands BEFORE the answer streams, so the badge can be right from the first
    // token instead of appearing only once the turn finishes. `done` repeats it, and the
    // persisted message carries `tier_used`, so this arm adds earliness, not truth.
    // `routing` is only present when intelligent routing ran; leave any existing value
    // alone rather than clearing it.
    return {
      ...a,
      tier: ev.data.tier,
      routing: ev.data.routing ?? a.routing,
    };
  }
  if (ev.event === "persona_originated") {
    // Spec C0: the persona started this message itself. The text lands in the bubble
    // like a chunk would (so the interleaved log keeps stream order), and `originated`
    // is what puts the "started this" badge on it. The persisted row carries the same
    // flag (`MessageView.originated`), so a reload shows the badge the live view did.
    return {
      ...a,
      working: false,
      originated: true,
      content: a.content + ev.data.content,
      events: [
        ...(a.events ?? []),
        { kind: "text", delta: ev.data.content } as const,
      ],
    };
  }
  if (ev.event === "done") {
    // Spec 31 (D-31-1/2): carry the model decision + budget snapshot alongside the
    // tier. NB: `done` never appears in the PERSISTED log (the worker routes tier to
    // the `tier_used` column and routing/budget ride only the live `done` payload —
    // P3-D-6 graceful degradation); this arm serves the live path only.
    return {
      ...a,
      tier: ev.data.tier,
      routing: ev.data.routing,
      budget: ev.data.budget,
    };
  }
  return a;
}

/**
 * Map ONE persisted `stream_events` entry to a `ChatEvent` the reducer consumes.
 *
 * Text deltas (`{kind: "text", delta}`) → a `chunk` event. RunEvent dumps
 * (`{type, step, data, timestamp}`) → `{event: type, data}` — `RunEvent.data` is
 * the bare chat payload (sse-types.ts §envelope). Returns `null` for an
 * unrecognised entry (defensive); types not handled by the reducer (e.g.
 * `reasoning`/`completed`) pass through harmlessly as no-op reductions.
 */
export function toChatEvent(entry: Record<string, unknown>): ChatEvent | null {
  if (entry.kind === "text") {
    return {
      event: "chunk",
      data: { delta: String(entry.delta ?? "") },
    } as ChatEvent;
  }
  if (typeof entry.type === "string") {
    return { event: entry.type, data: entry.data } as ChatEvent;
  }
  return null;
}

/** The persisted-message shape `persistedToView` reconstructs from (the GET `MessageView`). */
export interface PersistedMessage {
  id: string;
  role: string;
  content: string;
  tier_used?: string | null;
  events?: Record<string, unknown>[] | null;
  /** Spec C0: the persona started this message itself (the durable half of the badge). */
  originated?: boolean;
}

/**
 * Reconstruct a `ChatMessageView` from a persisted message — the single mapper
 * that replaces the two divergent text-only maps (`reload()` + `initialMessages`,
 * D-P3-4). Folds the persisted ordered log through the SAME `reduceChatEvent` the
 * live stream uses, so the reloaded interleaved view is identical to the live one.
 *
 * Absent / empty `events` (legacy / non-streamed / user / tool rows) → the
 * byte-exact text-only view (`{id, role, content, tier}`), the back-compat path
 * (criterion 5; the `tier_used` nullable-additive precedent).
 */
export function persistedToView(m: PersistedMessage): ChatMessageView {
  const tier = m.tier_used ?? undefined;
  // Spec C0: only ever set on a row the persona started; left absent otherwise so the
  // text-only view stays byte-exact for every ordinary reply (criterion 5).
  const originated = m.originated ? true : undefined;
  const events = m.events;
  if (!events || events.length === 0) {
    return { id: m.id, role: m.role, content: m.content, tier, originated };
  }
  let view: ChatMessageView = {
    id: m.id,
    role: m.role,
    content: "",
    tier,
    originated,
    events: [],
    tools: [],
  };
  for (const entry of events) {
    const ev = toChatEvent(entry);
    if (ev) view = reduceChatEvent(view, ev);
  }
  // A reconstructed turn is terminal — never show live indicators.
  return { ...view, streaming: false, working: false };
}
