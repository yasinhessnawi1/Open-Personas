/**
 * Spec C0 (part3 F11): a run whose persona sent the conclusion on as a message says so
 * in the run viewer, both while watched (the `persona_originated` run event) and when
 * reopened (the `persona_originated` note the worker stamps on the last persisted step).
 * The two reductions must land the note in the same place, or the run you watched and
 * the run you reopened tell different stories.
 */
import { describe, expect, it } from "vitest";
import type { RunStatusResponse } from "@/lib/api";
import { runEventToOutputContent } from "@/lib/normalisers/run-output";
import { runViewFromEvents, runViewFromSnapshot } from "@/lib/run";
import type { RunEvent } from "@/lib/sse-types";

const T = "2026-09-17T00:00:00Z";

const liveEvents: RunEvent[] = [
  { type: "started", step: -1, data: { task: "summarise" }, timestamp: T },
  { type: "reasoning", step: 0, data: { content: "thinking" }, timestamp: T },
  { type: "completed", step: 1, data: { output: "All done." }, timestamp: T },
  {
    type: "persona_originated",
    step: -1,
    data: {
      content: "All done.",
      persona_id: "astrid",
      persona_name: "Astrid",
      visual_ref: "",
      conversation_id: "conv_1",
    },
    timestamp: T,
  },
  {
    type: "finished",
    step: -1,
    data: { run_id: "r1", status: "completed" },
    timestamp: T,
  },
];

describe("runViewFromEvents: the persona spoke first (live)", () => {
  it("lands the note on the last step, pointing at the conversation it started", () => {
    const view = runViewFromEvents(liveEvents, { task: "summarise" });

    expect(view.status).toBe("completed");
    expect(view.steps).toHaveLength(2);
    expect(view.steps[0].notes).toBeUndefined();
    expect(view.steps[1].notes).toEqual([
      { kind: "persona_originated", conversationId: "conv_1" },
    ]);
  });

  it("is idempotent when the stream is replayed (one note per run, not one per replay)", () => {
    const view = runViewFromEvents([...liveEvents, ...liveEvents], {
      task: "summarise",
    });

    expect(view.steps[1].notes).toHaveLength(1);
  });

  it("keeps a guard note the same step already carried", () => {
    const withGuard: RunEvent[] = [
      liveEvents[0],
      liveEvents[2],
      {
        type: "context_pruned",
        step: 1,
        data: { before_tokens: 9000, after_tokens: 4000 },
        timestamp: T,
      },
      liveEvents[3],
    ];
    const view = runViewFromEvents(withGuard, { task: "summarise" });

    expect(view.steps[0].notes).toEqual([
      { kind: "context_pruned" },
      { kind: "persona_originated", conversationId: "conv_1" },
    ]);
  });
});

describe("runEventToOutputContent: the persona spoke first", () => {
  it("is a note on the step, never capability output", () => {
    expect(runEventToOutputContent(liveEvents[3])).toEqual([]);
  });
});

describe("runViewFromSnapshot: the persona spoke first (REOPENED)", () => {
  const snapshot = (notes: unknown): RunStatusResponse =>
    ({
      id: "r1",
      persona_id: "astrid",
      task: "summarise",
      status: "completed",
      output: "All done.",
      steps: [
        { type: "reasoning", content: "thinking", notes: [] },
        { type: "final", content: "All done.", notes },
      ],
    }) as unknown as RunStatusResponse;

  it("reads the stamp off the last persisted step", () => {
    const view = runViewFromSnapshot(
      snapshot([{ kind: "persona_originated", conversation_id: "conv_1" }]),
    );

    expect(view.steps[0].notes).toBeUndefined();
    expect(view.steps[1].notes).toEqual([
      { kind: "persona_originated", conversationId: "conv_1" },
    ]);
  });

  it("reads the same as the live stream for the same run", () => {
    const live = runViewFromEvents(liveEvents, { task: "summarise" });
    const reopened = runViewFromSnapshot(
      snapshot([{ kind: "persona_originated", conversation_id: "conv_1" }]),
    );

    expect(reopened.steps[1].notes).toEqual(live.steps[1].notes);
  });

  it("keeps the guard notes that were stamped before it", () => {
    const view = runViewFromSnapshot(
      snapshot([
        { kind: "call_skipped", tool: "web_search", guard: "cached_read" },
        { kind: "persona_originated", conversation_id: "conv_1" },
      ]),
    );

    expect(view.steps[1].notes).toEqual([
      { kind: "call_skipped", tool: "web_search" },
      { kind: "persona_originated", conversationId: "conv_1" },
    ]);
  });

  it("says nothing on a run whose persona never spoke first", () => {
    const view = runViewFromSnapshot(snapshot([]));

    expect(view.steps[1].notes).toBeUndefined();
  });
});
