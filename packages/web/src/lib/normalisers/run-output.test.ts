/**
 * Spec F4 T04 tests — RunEvent → OutputContent[] direct projection.
 *
 * Covers the single-event projection. The per-step accumulation lives
 * in `runViewFromEvents` at `lib/run.ts` and is tested separately in
 * `lib/run.test.ts` ("step.outputs derivation" describe).
 *
 * Cross-transport parity: the same payload classification produces the
 * IDENTICAL OutputContent variants as the chat normaliser. Shared via
 * `./_classify.ts`; transport-shape leakage (D-09-1) stops here.
 */

import { describe, expect, it } from "vitest";

import type { RunEvent } from "@/lib/sse-types";

import { runEventToOutputContent } from "./run-output";

const TS = "2026-06-07T11:00:00Z";

describe("runEventToOutputContent (T04)", () => {
  describe("timeline-state events emit []", () => {
    it.each([
      { type: "started", data: { task: "draft" } },
      { type: "tier", data: { tier: "mid" } },
      { type: "thinking", data: {} },
      { type: "asking_user", data: { question: "?" } },
      { type: "user_responded", data: {} },
      { type: "reasoning", data: { content: "x" } },
      { type: "completed", data: { output: "done" } },
      { type: "cancelled", data: {} },
      { type: "max_steps", data: { summary: "s" } },
      { type: "finished", data: { run_id: "r", status: "completed" } },
    ] as const)("type=$type emits no OutputContent", (probe) => {
      const event = {
        ...probe,
        step: 0,
        timestamp: TS,
      } as unknown as RunEvent;
      expect(runEventToOutputContent(event)).toEqual([]);
    });
  });

  describe("error events → run-level failure", () => {
    it("type=error projects to failure(operation=run)", () => {
      const event: RunEvent = {
        type: "error",
        step: 2,
        data: { message: "provider 500" },
        timestamp: TS,
      };
      expect(runEventToOutputContent(event)).toEqual([
        {
          kind: "failure",
          operation: "run",
          error_message: "provider 500",
        },
      ]);
    });

    it("run-level failure uses operation=run, distinct from per-tool failure", () => {
      const runErr: RunEvent = {
        type: "error",
        step: 1,
        data: { message: "boom" },
        timestamp: TS,
      };
      const toolErr: RunEvent = {
        type: "tool_result",
        step: 1,
        data: {
          tool_name: "code_execution",
          is_error: true,
          content: "tool boom",
        },
        timestamp: TS,
      };
      // Distinct operations → distinct renderer routing.
      expect(runEventToOutputContent(runErr)[0]).toMatchObject({
        operation: "run",
      });
      expect(runEventToOutputContent(toolErr)[0]).toMatchObject({
        operation: "code_execution",
      });
    });
  });

  describe("tool_calling → working states", () => {
    it("recognized capability tool emits working", () => {
      const event: RunEvent = {
        type: "tool_calling",
        step: 0,
        data: {
          tool_names: "code_execution",
          tool_calls: [
            { name: "code_execution", call_id: "c-1", args: { code: "1+1" } },
          ],
        },
        timestamp: TS,
      };
      expect(runEventToOutputContent(event)).toEqual([
        { kind: "working", operation: "code_exec", label: "code_execution" },
      ]);
    });

    it("unrecognized tool emits nothing", () => {
      const event: RunEvent = {
        type: "tool_calling",
        step: 0,
        data: {
          tool_names: "web_search",
          tool_calls: [{ name: "web_search", call_id: "c-1", args: {} }],
        },
        timestamp: TS,
      };
      expect(runEventToOutputContent(event)).toEqual([]);
    });
  });

  describe("tool_result → failure / produced files / result-block", () => {
    it("is_error=true → failure with operation=tool_name (tool-level)", () => {
      const event: RunEvent = {
        type: "tool_result",
        step: 0,
        data: {
          tool_name: "code_execution",
          is_error: true,
          content: "outcome=timeout exit_code=124",
        },
        timestamp: TS,
      };
      expect(runEventToOutputContent(event)).toEqual([
        {
          kind: "failure",
          operation: "code_execution",
          error_message: "outcome=timeout exit_code=124",
        },
      ]);
    });

    it("structured chart produced_file → inline-chart", () => {
      const event: RunEvent = {
        type: "tool_result",
        step: 0,
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "ok",
          produced_files: [
            { path: "charts/q1.png", size_bytes: 100, media_type: "image/png" },
          ],
        },
        timestamp: TS,
      };
      expect(runEventToOutputContent(event)).toEqual([
        {
          kind: "inline-chart",
          workspace_path: "charts/q1.png",
          media_type: "image/png",
        },
      ]);
    });

    it("structured Spec 16 doc → download-doc with size_bytes preserved", () => {
      const event: RunEvent = {
        type: "tool_result",
        step: 0,
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "ok",
          produced_files: [
            {
              path: "uploads/report.pdf",
              size_bytes: 999,
              media_type: "application/pdf",
            },
          ],
        },
        timestamp: TS,
      };
      expect(runEventToOutputContent(event)).toEqual([
        {
          kind: "download-doc",
          workspace_path: "uploads/report.pdf",
          media_type: "application/pdf",
          name: "report.pdf",
          size_bytes: 999,
        },
      ]);
    });

    it("missing produced_files → result-block (back-compat / pre-T02b)", () => {
      const event: RunEvent = {
        type: "tool_result",
        step: 0,
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "1+1=2",
        },
        timestamp: TS,
      };
      expect(runEventToOutputContent(event)).toEqual([
        {
          kind: "result-block",
          stdout: "1+1=2",
          truncated: false,
          language: "python",
        },
      ]);
    });

    it("truncated: true on the frame → result-block reports the cut (R9-163)", () => {
      // The tool cut its output to fit a budget and said so on the frame. Before
      // R9-163 the classifier hardcoded false here, so the result block's
      // upstream-truncation indicator could never fire on a live run.
      const event: RunEvent = {
        type: "tool_result",
        step: 0,
        data: {
          tool_name: "web_fetch",
          is_error: false,
          content: "first 4000 chars",
          truncated: true,
        },
        timestamp: TS,
      };
      expect(runEventToOutputContent(event)).toEqual([
        {
          kind: "result-block",
          stdout: "first 4000 chars",
          truncated: true,
          language: undefined,
        },
      ]);
    });
  });

  describe("D-09-1 transport-shape leakage stops at the normaliser", () => {
    it("renderer-facing OutputContent never carries RunEvent envelope fields", () => {
      const event: RunEvent = {
        type: "tool_result",
        step: 3,
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "x",
          produced_files: [
            { path: "charts/x.png", size_bytes: 1, media_type: "image/png" },
          ],
        },
        timestamp: TS,
      };
      const item = runEventToOutputContent(event)[0];
      expect(item).not.toHaveProperty("type");
      expect(item).not.toHaveProperty("step");
      expect(item).not.toHaveProperty("timestamp");
      expect(item).not.toHaveProperty("data");
    });
  });

  describe("cross-transport parity (matches chat-output classification)", () => {
    it("identical produced_file payload produces same OutputContent shape across transports", () => {
      // The shared `_classify.ts` guarantees the chat + run normalisers
      // produce IDENTICAL classification for the same produced_file. This
      // test pins that promise at the run-side boundary.
      const payload = {
        path: "charts/parity.png",
        size_bytes: 42,
        media_type: "image/png",
      };
      const runEvent: RunEvent = {
        type: "tool_result",
        step: 0,
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "ok",
          produced_files: [payload],
        },
        timestamp: TS,
      };
      const runOut = runEventToOutputContent(runEvent);
      expect(runOut).toEqual([
        {
          kind: "inline-chart",
          workspace_path: "charts/parity.png",
          media_type: "image/png",
        },
      ]);
      // The chat-side normaliser produces the IDENTICAL OutputContent (the
      // chat test asserts the same shape independently in its own file).
    });
  });
});
