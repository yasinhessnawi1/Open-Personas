import { describe, expect, it } from "vitest";
import type { ChatMessageView } from "@/components/chat/message-element";
import type { ChatEvent } from "@/lib/sse-types";
import {
  persistedToView,
  reduceChatEvent,
  toChatEvent,
} from "./reduce-chat-event";

/**
 * Spec P3 (P3-D-3 / P3-D-3b) — the shared reducer + persisted reconstruction.
 *
 * The gate proof for Group B is the LIVE-EQUIVALENCE test: folding a persisted
 * `stream_events` log through `persistedToView` yields the SAME `ChatMessageView`
 * as folding the equivalent live chat frames through `reduceChatEvent`. One
 * reducer → the live union and the persisted shape cannot drift (the anti-drift
 * guarantee). Plus the legacy/NULL byte-exact text-only back-compat (criterion 5).
 */

const base: ChatMessageView = {
  id: "m1",
  role: "assistant",
  content: "",
  events: [],
  tools: [],
};

// The live chat frames (bare-payload `ChatEvent`s) for: think → "Hello " →
// web_search call → result (with an artifact) → " world".
const liveFrames: ChatEvent[] = [
  { event: "thinking", data: {} },
  { event: "chunk", data: { delta: "Hello ", is_final: false } },
  {
    event: "tool_calling",
    data: {
      tool_names: "web_search",
      tool_calls: [
        {
          name: "web_search",
          call_id: "c1",
          args: { q: "x" },
          kind: "builtin",
        },
      ],
    },
  },
  {
    event: "tool_result",
    data: {
      tool_name: "web_search",
      is_error: false,
      content: "results",
      kind: "builtin",
      artifacts: [
        {
          workspace_path: "uploads/a.png",
          mime_type: "image/png",
          size_bytes: 10,
          rendered_inline: true,
        },
      ],
    },
  },
  { event: "chunk", data: { delta: "world", is_final: false } },
] as unknown as ChatEvent[];

// The SAME turn as it is PERSISTED in `stream_events`: RunEvent dumps
// (`{type, step, data, timestamp}`) interleaved with text deltas
// (`{kind: "text", delta}`), in emission order. NB: `thinking` is a transient
// live-only signal and is NOT asserted to persist; the durable log is the
// content + tool interleave.
const persistedLog: Record<string, unknown>[] = [
  { kind: "text", delta: "Hello " },
  {
    type: "tool_calling",
    step: -1,
    data: {
      tool_names: "web_search",
      tool_calls: [
        {
          name: "web_search",
          call_id: "c1",
          args: { q: "x" },
          kind: "builtin",
        },
      ],
    },
    timestamp: "2026-06-29T00:00:00Z",
  },
  {
    type: "tool_result",
    step: -1,
    data: {
      tool_name: "web_search",
      is_error: false,
      content: "results",
      kind: "builtin",
      artifacts: [
        {
          workspace_path: "uploads/a.png",
          mime_type: "image/png",
          size_bytes: 10,
          rendered_inline: true,
        },
      ],
    },
    timestamp: "2026-06-29T00:00:01Z",
  },
  { kind: "text", delta: "world" },
];

describe("reduceChatEvent — the shared live reducer", () => {
  it("builds content + interleaved events + derived tools from a live frame sequence", () => {
    const view = liveFrames.reduce((v, ev) => reduceChatEvent(v, ev), base);
    expect(view.content).toBe("Hello world");
    expect(view.events?.map((e) => e.kind)).toEqual([
      "text",
      "tool_call",
      "tool_result",
      "text",
    ]);
    expect(view.tools).toHaveLength(1);
    expect(view.tools?.[0]).toMatchObject({
      toolName: "web_search",
      result: "results",
      isError: false,
      pending: false,
      kind: "builtin",
    });
    // the artifact ref rides the tool_result event (the FileCard path)
    const tr = view.events?.find((e) => e.kind === "tool_result");
    expect(tr && "artifacts" in tr && tr.artifacts?.[0]?.workspace_path).toBe(
      "uploads/a.png",
    );
  });

  it("returns the view unchanged for an unhandled event (forward-compatible)", () => {
    const view = reduceChatEvent(base, {
      event: "reasoning",
      data: {},
    } as unknown as ChatEvent);
    expect(view).toEqual(base);
  });
});

describe("reduceChatEvent: an approval answered in chat", () => {
  it("carries the held note of an approval_resolved done onto the turn", () => {
    // The chat route answers a decision cue with a single `done` frame (no chunks); its
    // `note` says why the task waits (D-ORCH-11-18-d (2)). It was dropped on the floor.
    const done = {
      event: "done",
      data: {
        kind: "approval_resolved",
        outcome: "approve",
        executed: true,
        note: "held_paused",
        status: "consumed",
      },
    } as unknown as ChatEvent;
    expect(reduceChatEvent(base, done).approvalHeld).toBe("heldWhilePaused");
  });

  it("leaves an ordinary turn's done without a held note", () => {
    const done = {
      event: "done",
      data: { usage: {}, tier: "mid", format_hints: {} },
    } as ChatEvent;
    expect(reduceChatEvent(base, done).approvalHeld).toBeUndefined();
  });
});

describe("toChatEvent — persisted entry → ChatEvent", () => {
  it("maps a text delta to a chunk event", () => {
    expect(toChatEvent({ kind: "text", delta: "hi" })).toEqual({
      event: "chunk",
      data: { delta: "hi" },
    });
  });

  it("maps a RunEvent dump to {event: type, data} (the bare payload)", () => {
    const ev = toChatEvent({
      type: "tool_result",
      step: 0,
      data: { tool_name: "x" },
      timestamp: "",
    });
    expect(ev).toEqual({ event: "tool_result", data: { tool_name: "x" } });
  });

  it("returns null for an unrecognised entry", () => {
    expect(toChatEvent({ foo: "bar" })).toBeNull();
  });
});

describe("persistedToView — reconstruction", () => {
  it("LIVE-EQUIVALENCE: persisted log reconstructs the SAME view the live frames build", () => {
    const live = liveFrames.reduce((v, ev) => reduceChatEvent(v, ev), base);
    const reconstructed = persistedToView({
      id: "m1",
      role: "assistant",
      content: "Hello world",
      events: persistedLog,
    });
    // The durable fields the interleaved render depends on are identical.
    expect(reconstructed.content).toBe(live.content);
    expect(reconstructed.events).toEqual(live.events);
    expect(reconstructed.tools).toEqual(live.tools);
    // A reconstructed turn is terminal.
    expect(reconstructed.streaming).toBe(false);
    expect(reconstructed.working).toBe(false);
  });

  it("seeds the per-message tier from tier_used", () => {
    const v = persistedToView({
      id: "m1",
      role: "assistant",
      content: "x",
      tier_used: "frontier",
      events: [{ kind: "text", delta: "x" }],
    });
    expect(v.tier).toBe("frontier");
  });

  it("LEGACY: absent events → byte-exact text-only view (criterion 5)", () => {
    const legacy = persistedToView({
      id: "u1",
      role: "user",
      content: "hi",
      tier_used: null,
      events: null,
    });
    expect(legacy).toEqual({
      id: "u1",
      role: "user",
      content: "hi",
      tier: undefined,
    });
  });

  it("LEGACY: empty events list also degrades to text-only", () => {
    const legacy = persistedToView({
      id: "m9",
      role: "assistant",
      content: "done",
      events: [],
    });
    expect(legacy).toEqual({
      id: "m9",
      role: "assistant",
      content: "done",
      tier: undefined,
    });
  });
});
