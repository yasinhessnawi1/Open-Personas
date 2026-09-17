/**
 * Spec F4 T03 tests — chat SSE → OutputContent[] normaliser.
 *
 * Covers:
 *   - text-stream frames (chunk / done) → [].
 *   - tool_calling → `working` for recognized capability tools only.
 *   - tool_result → failure / produced-file classification / result-block fallback.
 *   - structural by-reference guarantee (1MB file is stored as workspace_path, never inlined).
 *   - D-09-1 transport-shape leakage stops at the normaliser.
 */

import { describe, expect, it } from "vitest";

import type { ChatEvent } from "@/lib/sse-types";

import { chatSseToOutputContent } from "./chat-output";

describe("chatSseToOutputContent (T03)", () => {
  describe("text-stream frames", () => {
    it("chunk events emit []", () => {
      const event: ChatEvent = {
        event: "chunk",
        data: { delta: "hello", is_final: false },
      };
      expect(chatSseToOutputContent(event)).toEqual([]);
    });

    it("done events emit []", () => {
      const event: ChatEvent = {
        event: "done",
        data: { usage: {}, tier: "mid", format_hints: {} },
      };
      expect(chatSseToOutputContent(event)).toEqual([]);
    });
  });

  describe("tool_calling → working states", () => {
    it("emits operation=image_gen for generate_image tool", () => {
      const event: ChatEvent = {
        event: "tool_calling",
        data: {
          tool_names: "generate_image",
          tool_calls: [
            {
              name: "generate_image",
              call_id: "c-1",
              args: { prompt: "a cat" },
            },
          ],
        },
      };
      expect(chatSseToOutputContent(event)).toEqual([
        { kind: "working", operation: "image_gen", label: "generate_image" },
      ]);
    });

    it("emits operation=code_exec for code_execution tool", () => {
      const event: ChatEvent = {
        event: "tool_calling",
        data: {
          tool_names: "code_execution",
          tool_calls: [
            { name: "code_execution", call_id: "c-1", args: { code: "1+1" } },
          ],
        },
      };
      expect(chatSseToOutputContent(event)[0]).toEqual({
        kind: "working",
        operation: "code_exec",
        label: "code_execution",
      });
    });

    it("emits operation=doc_gen for document_generation tool", () => {
      const event: ChatEvent = {
        event: "tool_calling",
        data: {
          tool_names: "document_generation",
          tool_calls: [
            { name: "document_generation", call_id: "c-1", args: {} },
          ],
        },
      };
      expect(chatSseToOutputContent(event)[0]?.kind).toBe("working");
    });

    it("filters out unrecognized tool names (no spurious working state)", () => {
      const event: ChatEvent = {
        event: "tool_calling",
        data: {
          tool_names: "web_search",
          tool_calls: [{ name: "web_search", call_id: "c-1", args: {} }],
        },
      };
      expect(chatSseToOutputContent(event)).toEqual([]);
    });

    it("emits multiple workings when multiple recognized tools fire", () => {
      const event: ChatEvent = {
        event: "tool_calling",
        data: {
          tool_names: "code_execution, generate_image",
          tool_calls: [
            { name: "code_execution", call_id: "c-1", args: {} },
            { name: "generate_image", call_id: "c-2", args: {} },
          ],
        },
      };
      const out = chatSseToOutputContent(event);
      expect(out).toHaveLength(2);
      expect(out[0]).toMatchObject({ kind: "working", operation: "code_exec" });
      expect(out[1]).toMatchObject({ kind: "working", operation: "image_gen" });
    });

    it("mixes recognized + unrecognized tools — only recognized emit working", () => {
      const event: ChatEvent = {
        event: "tool_calling",
        data: {
          tool_names: "web_search, code_execution",
          tool_calls: [
            { name: "web_search", call_id: "c-1", args: {} },
            { name: "code_execution", call_id: "c-2", args: {} },
          ],
        },
      };
      const out = chatSseToOutputContent(event);
      expect(out).toHaveLength(1);
      expect(out[0]).toMatchObject({ operation: "code_exec" });
    });
  });

  describe("tool_result → failure / produced files / result-block", () => {
    it("truncated: true on the frame → result-block reports the cut (R9-163)", () => {
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "web_fetch",
          is_error: false,
          content: "first 4000 chars",
          truncated: true,
        },
      };
      expect(chatSseToOutputContent(event)).toEqual([
        {
          kind: "result-block",
          stdout: "first 4000 chars",
          truncated: true,
          language: undefined,
        },
      ]);
    });

    it("is_error=true → failure variant with operation=tool_name", () => {
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "code_execution",
          is_error: true,
          content: "outcome=timeout exit_code=124",
        },
      };
      expect(chatSseToOutputContent(event)).toEqual([
        {
          kind: "failure",
          operation: "code_execution",
          error_message: "outcome=timeout exit_code=124",
        },
      ]);
    });

    it("Spec 17 chart (charts/ prefix + image/*) → inline-chart", () => {
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "ok",
          produced_files: [
            {
              path: "charts/q1.png",
              size_bytes: 1234,
              media_type: "image/png",
            },
          ],
        },
      };
      expect(chatSseToOutputContent(event)).toEqual([
        {
          kind: "inline-chart",
          workspace_path: "charts/q1.png",
          media_type: "image/png",
        },
      ]);
    });

    it("Spec 15 generated image (uploads/ + image/*) → inline-image", () => {
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "generate_image",
          is_error: false,
          content: "ok",
          produced_files: [
            {
              path: "uploads/abc.png",
              size_bytes: 5000,
              media_type: "image/png",
            },
          ],
        },
      };
      expect(chatSseToOutputContent(event)).toEqual([
        {
          kind: "inline-image",
          workspace_path: "uploads/abc.png",
          media_type: "image/png",
          alt: "abc.png",
        },
      ]);
    });

    it("Spec 16 docx (doc media-type) → download-doc with size_bytes preserved", () => {
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "ok",
          produced_files: [
            {
              path: "uploads/report.docx",
              size_bytes: 12345,
              media_type:
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            },
          ],
        },
      };
      const out = chatSseToOutputContent(event);
      expect(out).toEqual([
        {
          kind: "download-doc",
          workspace_path: "uploads/report.docx",
          media_type:
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          name: "report.docx",
          size_bytes: 12345,
        },
      ]);
    });

    it("multiple produced_files → multiple OutputContent preserved in order", () => {
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "ok",
          produced_files: [
            { path: "charts/q1.png", size_bytes: 100, media_type: "image/png" },
            {
              path: "summary.pdf",
              size_bytes: 200,
              media_type: "application/pdf",
            },
          ],
        },
      };
      const out = chatSseToOutputContent(event);
      expect(out).toHaveLength(2);
      expect(out[0]?.kind).toBe("inline-chart");
      expect(out[1]?.kind).toBe("download-doc");
    });

    it("missing produced_files (back-compat / pre-T02b) → result-block carrying content", () => {
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "web_search",
          is_error: false,
          content: "3 hits found",
        },
      };
      expect(chatSseToOutputContent(event)).toEqual([
        {
          kind: "result-block",
          stdout: "3 hits found",
          truncated: false,
          language: undefined,
        },
      ]);
    });

    it("empty produced_files [] → result-block (treated as absence)", () => {
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "no files produced",
          produced_files: [],
        },
      };
      expect(chatSseToOutputContent(event)).toEqual([
        {
          kind: "result-block",
          stdout: "no files produced",
          truncated: false,
          language: "python",
        },
      ]);
    });

    it("code_execution result-block defaults language=python", () => {
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "Hello world",
        },
      };
      expect(chatSseToOutputContent(event)[0]).toMatchObject({
        kind: "result-block",
        language: "python",
      });
    });

    it("non-charts/ image (bare path) falls through to inline-image", () => {
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "ok",
          produced_files: [
            { path: "photo.jpg", size_bytes: 9999, media_type: "image/jpeg" },
          ],
        },
      };
      expect(chatSseToOutputContent(event)).toEqual([
        {
          kind: "inline-image",
          workspace_path: "photo.jpg",
          media_type: "image/jpeg",
          alt: "photo.jpg",
        },
      ]);
    });

    it("unknown media-type falls through to download-doc with size_bytes", () => {
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "ok",
          produced_files: [
            {
              path: "random.bin",
              size_bytes: 42,
              media_type: "application/octet-stream",
            },
          ],
        },
      };
      expect(chatSseToOutputContent(event)[0]).toEqual({
        kind: "download-doc",
        workspace_path: "random.bin",
        media_type: "application/octet-stream",
        name: "random.bin",
        size_bytes: 42,
      });
    });

    it("null media_type defaults to application/octet-stream (download-doc)", () => {
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "ok",
          produced_files: [
            { path: "x.unknown", size_bytes: 1, media_type: null },
          ],
        },
      };
      expect(chatSseToOutputContent(event)[0]?.kind).toBe("download-doc");
    });

    it("nested subdirectory image routes to inline-image (defensive fallback)", () => {
      // A skill that writes to an unexpected subdir (e.g. analysis/) still
      // surfaces correctly — non-charts/ + image/* → inline-image. Less
      // useful than a real chart, but never crashes the render.
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "ok",
          produced_files: [
            {
              path: "analysis/scratch.png",
              size_bytes: 1,
              media_type: "image/png",
            },
          ],
        },
      };
      expect(chatSseToOutputContent(event)[0]).toMatchObject({
        kind: "inline-image",
        workspace_path: "analysis/scratch.png",
      });
    });
  });

  describe("structural by-reference guarantee (T22 mirror)", () => {
    it("a 1MB produced_file stays as a workspace_path string; never inlined", () => {
      // The OutputContent shape has no `bytes` field by construction; this
      // test pins the structural guarantee at the wire-shape boundary.
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "ok",
          produced_files: [
            {
              path: "charts/big.png",
              size_bytes: 1024 * 1024,
              media_type: "image/png",
            },
          ],
        },
      };
      const out = chatSseToOutputContent(event);
      const serialised = JSON.stringify(out);
      // OutputContent serialised size is bounded — must NOT scale with
      // the referenced file size (~150 bytes regardless of size_bytes).
      expect(serialised.length).toBeLessThan(500);
      expect(out[0]).toMatchObject({
        kind: "inline-chart",
        workspace_path: "charts/big.png",
      });
    });

    it("ten 5MB files keep the serialised normaliser output linear in count", () => {
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "ok",
          produced_files: Array.from({ length: 10 }, (_, i) => ({
            path: `charts/chart-${i}.png`,
            size_bytes: 5 * 1024 * 1024,
            media_type: "image/png",
          })),
        },
      };
      const out = chatSseToOutputContent(event);
      const serialised = JSON.stringify(out);
      expect(out).toHaveLength(10);
      // 10 references × ~120 bytes per ≈ 1.5 KB. The cap is generous to
      // tolerate path-string variance; the structural point is "linear in
      // count, NOT in bytes_referenced".
      expect(serialised.length).toBeLessThan(2_000);
    });
  });

  describe("D-09-1 transport-shape leakage stops at the normaliser", () => {
    it("renderer-facing OutputContent never carries chat-frame fields", () => {
      const event: ChatEvent = {
        event: "tool_result",
        data: {
          tool_name: "code_execution",
          is_error: false,
          content: "x",
          produced_files: [
            { path: "charts/x.png", size_bytes: 1, media_type: "image/png" },
          ],
        },
      };
      const item = chatSseToOutputContent(event)[0];
      expect(item).not.toHaveProperty("event");
      expect(item).not.toHaveProperty("tool_name");
      expect(item).not.toHaveProperty("is_error");
      expect(item).not.toHaveProperty("content");
    });
  });
});
