import { describe, expect, it } from "vitest";
import type { RunStatusResponse } from "@/lib/api";
import { isTerminal, runViewFromEvents, runViewFromSnapshot } from "./run";
import type { RunEvent } from "./sse-types";

const TS = "2026-05-29T12:00:00Z";

// Typed RunEvent builders (mirror the API's RunEvent constructors).
const ev = {
  started: (task: string): RunEvent => ({
    type: "started",
    step: -1,
    data: { task },
    timestamp: TS,
  }),
  tier: (tier: string): RunEvent => ({
    type: "tier",
    step: -1,
    data: { tier },
    timestamp: TS,
  }),
  thinking: (step: number): RunEvent => ({
    type: "thinking",
    step,
    data: {},
    timestamp: TS,
  }),
  toolCalling: (step: number, name: string, callId: string): RunEvent => ({
    type: "tool_calling",
    step,
    data: {
      tool_names: name,
      tool_calls: [{ name, call_id: callId, args: { q: "x" } }],
    },
    timestamp: TS,
  }),
  toolResult: (step: number, name: string, content: string): RunEvent => ({
    type: "tool_result",
    step,
    data: { tool_name: name, is_error: false, content },
    timestamp: TS,
  }),
  asking: (step: number, question: string): RunEvent => ({
    type: "asking_user",
    step,
    data: { question },
    timestamp: TS,
  }),
  responded: (step: number): RunEvent => ({
    type: "user_responded",
    step,
    data: {},
    timestamp: TS,
  }),
  completed: (step: number, output: string): RunEvent => ({
    type: "completed",
    step,
    data: { output },
    timestamp: TS,
  }),
  finished: (status: string): RunEvent => ({
    type: "finished",
    step: 9,
    data: { run_id: "run_1", status },
    timestamp: TS,
  }),
  // P2 — the activity contract.
  activityStart: (step: number, id: string, name: string): RunEvent => ({
    type: "activity_start",
    step,
    data: {
      activity_id: id,
      kind: name.startsWith("mcp:") ? "mcp" : "web",
      name,
      label: "Searching the web",
      args_summary: { q: "x" },
    },
    timestamp: TS,
  }),
  activityEnd: (step: number, id: string, status: string): RunEvent => ({
    type: "activity_end",
    step,
    data: {
      activity_id: id,
      status,
      duration_ms: 9,
      is_error: status === "error" || status === "awaiting_approval",
    },
    timestamp: TS,
  }),
};

describe("runViewFromEvents", () => {
  it("reduces a multi-step run into an ordered timeline", () => {
    const view = runViewFromEvents(
      [
        ev.started("Plan a trip"),
        ev.tier("mid"),
        ev.thinking(0),
        ev.toolCalling(0, "web_search", "c1"),
        ev.toolResult(0, "web_search", "found 3 results"),
        ev.completed(1, "Here is your plan."),
        ev.finished("completed"),
      ],
      { task: "fallback" },
    );

    expect(view.task).toBe("Plan a trip");
    expect(view.tier).toBe("mid");
    expect(view.status).toBe("completed");
    expect(view.steps).toHaveLength(2);

    const [s0, s1] = view.steps;
    expect(s0.step).toBe(0);
    expect(s0.tools).toHaveLength(1);
    expect(s0.tools[0]).toMatchObject({
      toolName: "web_search",
      result: "found 3 results",
      pending: false,
    });
    expect(s1.final).toBe("Here is your plan.");
    expect(view.output).toBe("Here is your plan.");
  });

  it("is idempotent when events are replayed (reconnect / StrictMode)", () => {
    const events = [
      ev.toolCalling(0, "web_search", "c1"),
      ev.toolResult(0, "web_search", "ok"),
    ];
    // Replaying the same events (as on an SSE reconnect) must not duplicate tools.
    const view = runViewFromEvents([...events, ...events], { task: "t" });
    expect(view.steps).toHaveLength(1);
    expect(view.steps[0].tools).toHaveLength(1);
    expect(view.steps[0].tools[0].pending).toBe(false);
  });

  it("tracks an ask-user question and its answer", () => {
    const open = runViewFromEvents([ev.asking(0, "Which city?")], {
      task: "t",
    });
    expect(open.steps[0].question).toBe("Which city?");
    expect(open.steps[0].answered).toBe(false);

    const answered = runViewFromEvents(
      [ev.asking(0, "Which city?"), ev.responded(0)],
      { task: "t" },
    );
    expect(answered.steps[0].answered).toBe(true);
  });
});

describe("runViewFromEvents — P2 activity no-double-render", () => {
  // One step-0 call where BOTH the activity contract AND tool_calling/tool_result stream
  // for the same call (P2-D-3 keep-both). The reducer must produce ONE tool card and ONE
  // live-activity entry — not two cards.
  const oneCall = [
    ev.started("find rent law"),
    ev.toolCalling(0, "web_search", "c1"),
    ev.activityStart(0, "a1", "web_search"),
    ev.activityEnd(0, "a1", "ok"),
    ev.toolResult(0, "web_search", "results"),
  ];

  it("renders ONE tool card + ONE activity entry per call (not two cards)", () => {
    const view = runViewFromEvents(oneCall, { task: "find rent law" });
    expect(view.steps).toHaveLength(1);
    const step = view.steps[0];
    // Card channel: one tool, sourced from tool_result.
    expect(step.tools).toHaveLength(1);
    expect(step.tools[0]).toMatchObject({
      toolName: "web_search",
      pending: false,
    });
    // Activity channel: one resolved live-state, SEPARATE from tools.
    expect(step.activities).toHaveLength(1);
    expect(step.activities?.[0]).toMatchObject({
      activityId: "a1",
      kind: "web",
      status: "ok",
    });
  });

  it("is idempotent on reattach replay — re-streaming the log never doubles", () => {
    const replayed = runViewFromEvents([...oneCall, ...oneCall], {
      task: "find rent law",
    });
    expect(replayed.steps).toHaveLength(1);
    expect(replayed.steps[0].tools).toHaveLength(1);
    expect(replayed.steps[0].activities).toHaveLength(1);
    expect(replayed.steps[0].activities?.[0].status).toBe("ok");
  });

  it("carries an awaiting_approval activity end (A3 gate) onto the step", () => {
    const view = runViewFromEvents(
      [
        ev.started("t"),
        ev.activityStart(0, "a1", "spend_money"),
        ev.activityEnd(0, "a1", "awaiting_approval"),
      ],
      { task: "t" },
    );
    expect(view.steps[0].activities?.[0].status).toBe("awaiting_approval");
  });

  it("preserves per-step ordering after replay (no reorder/drop)", () => {
    const twoSteps = [
      ...oneCall,
      ev.activityStart(1, "a2", "code_execution"),
      ev.activityEnd(1, "a2", "ok"),
    ];
    const view = runViewFromEvents([...twoSteps, ...twoSteps], { task: "t" });
    expect(view.steps.map((s) => s.step)).toEqual([0, 1]);
    expect(view.steps[1].activities?.[0]).toMatchObject({
      activityId: "a2",
      status: "ok",
    });
  });
});

describe("runViewFromEvents: W1 guard notes", () => {
  const skipped = (step: number, tool: string, guard: string): RunEvent => ({
    type: "call_skipped",
    step,
    data: { tool, guard },
    timestamp: TS,
  });
  const pruned = (step: number): RunEvent => ({
    type: "context_pruned",
    step,
    data: { before_tokens: 9000, after_tokens: 4000 },
    timestamp: TS,
  });

  it("carries a skipped call onto the step it happened on", () => {
    const view = runViewFromEvents(
      [ev.started("Research"), skipped(0, "web_search", "cached_read")],
      { task: "fallback" },
    );

    expect(view.steps[0].notes).toEqual([
      { kind: "call_skipped", tool: "web_search" },
    ]);
  });

  it("keeps every skipped call on a step that batched several", () => {
    const view = runViewFromEvents(
      [
        ev.started("Research"),
        skipped(0, "web_search", "cached_read"),
        skipped(0, "file_read", "repeat_error"),
      ],
      { task: "fallback" },
    );

    expect(view.steps[0].notes).toHaveLength(2);
  });

  it("carries a pruned context onto its step", () => {
    const view = runViewFromEvents([ev.started("Research"), pruned(1)], {
      task: "fallback",
    });

    expect(view.steps[0].notes).toEqual([{ kind: "context_pruned" }]);
  });

  it("leaves steps without guard activity with no notes at all", () => {
    const view = runViewFromEvents(
      [ev.started("Research"), ev.toolResult(0, "web_search", "found it")],
      { task: "fallback" },
    );

    expect(view.steps[0].notes).toBeUndefined();
  });
});

describe("runViewFromSnapshot", () => {
  it("reduces a running snapshot (RunEvent event-log shape)", () => {
    const snap: RunStatusResponse = {
      id: "run_1",
      persona_id: "p1",
      task: "Do a thing",
      status: "running",
      steps: [
        ev.thinking(0) as unknown as Record<string, unknown>,
        ev.toolCalling(0, "web_search", "c1") as unknown as Record<
          string,
          unknown
        >,
      ],
    };
    const view = runViewFromSnapshot(snap);
    expect(view.status).toBe("running");
    expect(view.steps).toHaveLength(1);
    expect(view.steps[0].tools[0].pending).toBe(true);
  });

  it("reduces a terminal snapshot (persisted Step shape), pairing results by call_id", () => {
    const snap: RunStatusResponse = {
      id: "run_1",
      persona_id: "p1",
      task: "Research",
      status: "completed",
      output: "Final answer",
      steps: [
        {
          type: "tool_call",
          tool_calls: [{ name: "web_search", call_id: "c1", args: { q: "x" } }],
          results: [
            {
              tool_name: "web_search",
              call_id: "c1",
              content: "hit",
              is_error: false,
            },
          ],
          tier_used: "small",
        },
        { type: "final", content: "Final answer", tier_used: "mid" },
      ],
    };
    const view = runViewFromSnapshot(snap);
    expect(view.status).toBe("completed");
    expect(view.steps).toHaveLength(2);
    expect(view.steps[0].tools[0]).toMatchObject({
      toolName: "web_search",
      result: "hit",
      isError: false,
      pending: false,
    });
    expect(view.steps[0].tier).toBe("small");
    expect(view.steps[1].final).toBe("Final answer");
  });

  it("prefers the snapshot's top-level status over an event-derived one", () => {
    // A running snapshot whose event-log already contains a completed event:
    // the authoritative top-level status still wins (defensive).
    const snap: RunStatusResponse = {
      id: "run_1",
      persona_id: "p1",
      task: "t",
      status: "cancelled",
      steps: [ev.completed(0, "x") as unknown as Record<string, unknown>],
    };
    expect(runViewFromSnapshot(snap).status).toBe("cancelled");
  });

  it("handles an empty run with no steps", () => {
    const snap: RunStatusResponse = {
      id: "run_1",
      persona_id: "p1",
      task: "t",
      status: "running",
      steps: [],
    };
    const view = runViewFromSnapshot(snap);
    expect(view.steps).toEqual([]);
  });
});

describe("isTerminal", () => {
  it("classifies run statuses", () => {
    expect(isTerminal("running")).toBe(false);
    expect(isTerminal("completed")).toBe(true);
    expect(isTerminal("cancelled")).toBe(true);
    expect(isTerminal("max_steps_reached")).toBe(true);
    expect(isTerminal("error")).toBe(true);
  });
});

// ========================================== Spec F4 T04 — step.outputs derivation

describe("runViewFromEvents → step.outputs derivation (T04)", () => {
  // Custom builders that exercise the F4 capability path. The shared `ev`
  // builders above target the timeline-state path (web_search etc.); these
  // hit code_execution / generate_image / document_generation.

  const capExec = (step: number, callId: string): RunEvent => ({
    type: "tool_calling",
    step,
    data: {
      tool_names: "code_execution",
      tool_calls: [
        { name: "code_execution", call_id: callId, args: { code: "x" } },
      ],
    },
    timestamp: TS,
  });

  const capImg = (step: number, callId: string): RunEvent => ({
    type: "tool_calling",
    step,
    data: {
      tool_names: "generate_image",
      tool_calls: [
        {
          name: "generate_image",
          call_id: callId,
          args: { prompt: "a cat" },
        },
      ],
    },
    timestamp: TS,
  });

  const execResultWithProduced = (
    step: number,
    produced: Array<{
      path: string;
      size_bytes: number;
      media_type: string | null;
    }>,
  ): RunEvent => ({
    type: "tool_result",
    step,
    data: {
      tool_name: "code_execution",
      is_error: false,
      content: "ok",
      produced_files: produced,
    },
    timestamp: TS,
  });

  it("tool_calling seeds outputs with one `working` per recognized capability tool", () => {
    const view = runViewFromEvents([capExec(0, "c1")], { task: "t" });
    expect(view.steps[0].outputs).toEqual([
      { kind: "working", operation: "code_exec", label: "code_execution" },
    ]);
  });

  it("unrecognized tool emits NO outputs (web_search surfaces via tool-card only)", () => {
    const view = runViewFromEvents([ev.toolCalling(0, "web_search", "c1")], {
      task: "t",
    });
    expect(view.steps[0].outputs).toEqual([]);
    // Tool-card path still populated for the unrecognized tool.
    expect(view.steps[0].tools).toHaveLength(1);
    expect(view.steps[0].tools[0]).toMatchObject({ toolName: "web_search" });
  });

  it("tool_result with produced_files replaces matching `working` with classified outputs", () => {
    const view = runViewFromEvents(
      [
        capExec(0, "c1"),
        execResultWithProduced(0, [
          { path: "charts/q1.png", size_bytes: 100, media_type: "image/png" },
        ]),
      ],
      { task: "t" },
    );
    expect(view.steps[0].outputs).toEqual([
      {
        kind: "inline-chart",
        workspace_path: "charts/q1.png",
        media_type: "image/png",
      },
    ]);
  });

  it("tool_result with multiple produced_files expands the slot in order", () => {
    const view = runViewFromEvents(
      [
        capExec(0, "c1"),
        execResultWithProduced(0, [
          { path: "charts/q1.png", size_bytes: 100, media_type: "image/png" },
          {
            path: "uploads/summary.pdf",
            size_bytes: 200,
            media_type: "application/pdf",
          },
        ]),
      ],
      { task: "t" },
    );
    expect(view.steps[0].outputs).toHaveLength(2);
    expect(view.steps[0].outputs[0].kind).toBe("inline-chart");
    expect(view.steps[0].outputs[1].kind).toBe("download-doc");
  });

  it("tool_result is_error replaces matching `working` with failure", () => {
    const errResult: RunEvent = {
      type: "tool_result",
      step: 0,
      data: {
        tool_name: "code_execution",
        is_error: true,
        content: "outcome=timeout",
      },
      timestamp: TS,
    };
    const view = runViewFromEvents([capExec(0, "c1"), errResult], {
      task: "t",
    });
    expect(view.steps[0].outputs).toEqual([
      {
        kind: "failure",
        operation: "code_execution",
        error_message: "outcome=timeout",
      },
    ]);
  });

  it("tool_result without produced_files falls back to result-block (pre-T02b safety net)", () => {
    const result: RunEvent = {
      type: "tool_result",
      step: 0,
      data: {
        tool_name: "code_execution",
        is_error: false,
        content: "Hello",
      },
      timestamp: TS,
    };
    const view = runViewFromEvents([capExec(0, "c1"), result], { task: "t" });
    expect(view.steps[0].outputs).toEqual([
      {
        kind: "result-block",
        stdout: "Hello",
        truncated: false,
        language: "python",
      },
    ]);
  });

  it("two recognized tools in one step: each working resolved by its matching tool_result", () => {
    const tc: RunEvent = {
      type: "tool_calling",
      step: 0,
      data: {
        tool_names: "code_execution, generate_image",
        tool_calls: [
          { name: "code_execution", call_id: "c1", args: {} },
          { name: "generate_image", call_id: "c2", args: { prompt: "p" } },
        ],
      },
      timestamp: TS,
    };
    const view = runViewFromEvents(
      [
        tc,
        execResultWithProduced(0, [
          { path: "charts/x.png", size_bytes: 1, media_type: "image/png" },
        ]),
        // generate_image still working — only one tool_result resolved.
      ],
      { task: "t" },
    );
    expect(view.steps[0].outputs).toHaveLength(2);
    expect(view.steps[0].outputs[0].kind).toBe("inline-chart");
    expect(view.steps[0].outputs[1]).toMatchObject({
      kind: "working",
      operation: "image_gen",
    });
  });

  it("top-level run error does NOT push to step.outputs (surfaces via st.error)", () => {
    const err: RunEvent = {
      type: "error",
      step: 0,
      data: { message: "provider 500" },
      timestamp: TS,
    };
    const view = runViewFromEvents([capExec(0, "c1"), err], { task: "t" });
    // step.outputs keeps the working state untouched — the run error surfaces
    // via st.error and view.error, not via the capability output channel.
    expect(view.steps[0].outputs).toEqual([
      { kind: "working", operation: "code_exec", label: "code_execution" },
    ]);
    expect(view.steps[0].error).toBe("provider 500");
    expect(view.error).toBe("provider 500");
  });

  it("idempotent under replay (SSE reconnect re-seeds events)", () => {
    const events = [
      capExec(0, "c1"),
      execResultWithProduced(0, [
        { path: "charts/x.png", size_bytes: 1, media_type: "image/png" },
      ]),
    ];
    const replayed = runViewFromEvents([...events, ...events], { task: "t" });
    expect(replayed.steps[0].outputs).toEqual([
      {
        kind: "inline-chart",
        workspace_path: "charts/x.png",
        media_type: "image/png",
      },
    ]);
  });

  it("empty step has empty outputs (no false-positive working state)", () => {
    const view = runViewFromEvents([ev.thinking(0)], { task: "t" });
    expect(view.steps[0].outputs).toEqual([]);
  });

  it("Spec 15 generate_image working state resolved via uploads/<blake2b>.png", () => {
    const result: RunEvent = {
      type: "tool_result",
      step: 0,
      data: {
        tool_name: "generate_image",
        is_error: false,
        content: "ok",
        produced_files: [
          {
            path: "uploads/abc.png",
            size_bytes: 500,
            media_type: "image/png",
          },
        ],
      },
      timestamp: TS,
    };
    const view = runViewFromEvents([capImg(0, "c1"), result], { task: "t" });
    expect(view.steps[0].outputs).toEqual([
      {
        kind: "inline-image",
        workspace_path: "uploads/abc.png",
        media_type: "image/png",
        alt: "abc.png",
      },
    ]);
  });
});
