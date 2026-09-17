/**
 * Spec F4 T02 — `OutputContent` discriminated union (D-F4-X-renderer-normaliser-shape).
 *
 * The single normalised shape that both transports — chat SSE bare-payload
 * frames AND run SSE RunEvent envelopes — reduce into. Renderers consume
 * ONLY this union; transport-shape leakage stops at the normaliser per
 * D-09-1. Two mapping functions (`chatSseToOutputContent` +
 * `runEventToOutputContent`) are the SOLE place that knows the wire shape.
 *
 * Discriminator: `kind` (mirrors the D-F2-15 `MessageEvent` precedent at
 * `packages/web/src/components/chat/message-element.tsx:64-77`).
 *
 * Variants are additive — when a new capability lands (e.g. inline-audio
 * from a future TTS spec), add a new `kind` variant; never overload an
 * existing one. Matches the additive-extension precedent set by D-01-12 /
 * D-02-2 / D-03-3 / D-12-14 / D-13-X-now.
 *
 * The Zod schemas below pair 1:1 with the TypeScript types. `.strict()`
 * per variant mirrors Pydantic v2 `extra="forbid"` so the normaliser
 * fails loudly when the wire shape drifts.
 */

import { z } from "zod";

// -----------------------------------------------------------------------------
// TypeScript discriminated union
// -----------------------------------------------------------------------------

/** A persona-generated image (Spec 15) rendered inline at the message position. */
export interface InlineImageOutput {
  kind: "inline-image";
  /** Workspace-relative path, e.g. `uploads/<blake2b>.png` (Spec 15) — the
   *  GET endpoint at `image_service.fetch:300` resolves via the slash-aware
   *  rule. */
  workspace_path: string;
  /** IANA media type from the producer (image/png, image/jpeg, image/webp). */
  media_type: string;
  /** Required for a11y. Falls back to filename when the producer didn't supply. */
  alt: string;
  /** Optional caption rendered beneath the image. */
  caption?: string;
}

/** An analysis chart (Spec 17) rendered inline at the persona's prose finding. */
export interface InlineChartOutput {
  kind: "inline-chart";
  /** Spec 17 charts persist at `charts/<id>.png` per D-17-X-inline-hint-shape;
   *  the `charts/` prefix is load-bearing for the inline-vs-download
   *  discriminator. */
  workspace_path: string;
  media_type: string;
  /** Optional adjacent assistant text contextualising the chart (the persona's
   *  prose finding paired with the visual). */
  prose_context?: string;
}

/** A generated document (Spec 16) surfaced as a download chip. */
export interface DownloadDocOutput {
  kind: "download-doc";
  /** Workspace-relative path. Post-T02c (D-F4-X-bare-ref-resolution), Spec 16
   *  docs persist under `uploads/<filename>.<ext>` and resolve verbatim via
   *  the slash-aware GET surface. */
  workspace_path: string;
  media_type: string;
  /** Display name (basename of workspace_path or producer-supplied). */
  name: string;
  /** From `produced_files[].size_bytes` after D-F4-X-event-kind-for-produced-files
   *  amendment. Omitted on pre-amendment frames (back-compat safety net). */
  size_bytes?: number;
}

/** Code-execution stdout (Spec 12) presented as a legible result block. */
export interface ResultBlockOutput {
  kind: "result-block";
  /** Full `ToolResult.content` string: stdout + rendered `--- outcome ---`
   *  + rendered `-- files --`. The renderer chooses how to crop / expand. */
  stdout: string;
  /** Producer-reported truncation: the tool cut its output to fit a budget.
   *  Both transports carry it on the `tool_result` frame since R9-163 (omitted
   *  when false); a frame without the key reads as false. */
  truncated: boolean;
  /** Optional code echoed for F1 instrument-transparency (D-F4-1 collapsible
   *  default-collapsed); rendered via Shiki dynamic-import per
   *  D-F4-X-instrument-transparency-affordance. */
  code?: string;
  /** `python` for code_execution; informs the Shiki highlighter language
   *  pick. Defaulted by the normaliser when the tool name is recognised. */
  language?: string;
}

/**
 * Spec 28 — a persisted tool artifact rendered as an inline file card that
 * opens the right-panel renderer (D-28-5/6). Image artifacts < threshold show
 * a thumbnail above the card; diagram SVG < threshold renders inline. Fed by
 * `ToolResult.artifacts` (the unified rich-output path), in preference to the
 * legacy `produced_files` → inline-image/chart/download-doc classification.
 */
export interface FileCardOutput {
  kind: "file-card";
  /** Workspace-relative path; the GET `/uploads/{ref}` route serves the bytes
   *  (download_url is derived from this — D-28-10, no new endpoint). */
  workspace_path: string;
  /** IANA media type — drives the renderer dispatch (incl. text/vnd.mermaid
   *  and text/vnd.graphviz for render_diagram, D-28-X-render-diagram-mime). */
  media_type: string;
  /** Display name (basename of workspace_path). */
  name: string;
  /** Size of the persisted bytes. */
  size_bytes: number;
  /** Frontend hint: render inline (image thumbnail / inline SVG) above the
   *  card vs. card-only. The renderer still applies the size thresholds. */
  rendered_inline: boolean;
}

/** A slow operation is in flight (image gen / code exec / doc gen). */
export interface WorkingOutput {
  kind: "working";
  /** Closed enum (never bare "Loading...") — drives the contextual i18n
   *  label inside `<WorkingState>` per D-F4-5. */
  operation: "image_gen" | "code_exec" | "doc_gen";
  /** Optional human-readable label (e.g. tool name from the `tool_calling`
   *  event — "running code_execution..."); falls back to operation-default
   *  copy when absent. */
  label?: string;
}

/** A tool dispatch (or top-level run) failed. */
export interface FailureOutput {
  kind: "failure";
  /** Tool name for tool failures; "run" for top-level RunEvent.error
   *  (operation="run" distinguishes whole-run failure UX from per-tool). */
  operation: string;
  /** Human-readable error string. For sandbox errors, this is the
   *  `--- outcome ---` line + stderr trailer from `ToolResult.content`. */
  error_message: string;
}

/**
 * One unit of rich output ready for renderer dispatch (D-F4-X-renderer-normaliser-shape).
 *
 * Six variants verified end-to-end through the chat + run normaliser sketches
 * in `docs/specs/phase2/spec_F4/research.md` §R-F4-2. The dispatcher at
 * `packages/web/src/components/chat/output/dispatcher.tsx` (T09) reads `kind`
 * and routes to the right renderer.
 */
export type OutputContent =
  | InlineImageOutput
  | InlineChartOutput
  | DownloadDocOutput
  | FileCardOutput
  | ResultBlockOutput
  | WorkingOutput
  | FailureOutput;

// -----------------------------------------------------------------------------
// Zod schemas — boundary validation
// -----------------------------------------------------------------------------

const inlineImageSchema = z
  .object({
    kind: z.literal("inline-image"),
    workspace_path: z.string().min(1),
    media_type: z.string().min(1),
    alt: z.string(),
    caption: z.string().optional(),
  })
  .strict();

const inlineChartSchema = z
  .object({
    kind: z.literal("inline-chart"),
    workspace_path: z.string().min(1),
    media_type: z.string().min(1),
    prose_context: z.string().optional(),
  })
  .strict();

const downloadDocSchema = z
  .object({
    kind: z.literal("download-doc"),
    workspace_path: z.string().min(1),
    media_type: z.string().min(1),
    name: z.string().min(1),
    size_bytes: z.number().int().nonnegative().optional(),
  })
  .strict();

const resultBlockSchema = z
  .object({
    kind: z.literal("result-block"),
    stdout: z.string(),
    truncated: z.boolean(),
    code: z.string().optional(),
    language: z.string().optional(),
  })
  .strict();

const fileCardSchema = z
  .object({
    kind: z.literal("file-card"),
    workspace_path: z.string().min(1),
    media_type: z.string().min(1),
    name: z.string().min(1),
    size_bytes: z.number().int().nonnegative(),
    rendered_inline: z.boolean(),
  })
  .strict();

const workingSchema = z
  .object({
    kind: z.literal("working"),
    operation: z.enum(["image_gen", "code_exec", "doc_gen"]),
    label: z.string().optional(),
  })
  .strict();

const failureSchema = z
  .object({
    kind: z.literal("failure"),
    operation: z.string().min(1),
    error_message: z.string().min(1),
  })
  .strict();

/**
 * Discriminated-union Zod schema for `OutputContent`. Use at the SSE
 * boundary inside the normaliser; renderers should rely on the
 * TypeScript types directly (the schema is for boundary validation,
 * not hot render paths).
 */
export const outputContentSchema = z.discriminatedUnion("kind", [
  inlineImageSchema,
  inlineChartSchema,
  downloadDocSchema,
  fileCardSchema,
  resultBlockSchema,
  workingSchema,
  failureSchema,
]);

/** Inferred type for callers preferring zod's inference shape. */
export type OutputContentParsed = z.infer<typeof outputContentSchema>;
