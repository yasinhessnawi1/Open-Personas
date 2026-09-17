/**
 * Spec F4 — shared classification helpers consumed by BOTH the chat
 * SSE normaliser (T03 — `./chat-output.ts`) AND the RunEvent normaliser
 * (T04 — `./run-output.ts`).
 *
 * D-09-1 makes the transports diverge (chat = bare payload; run = nested
 * under `.data`) but the per-payload classification logic is IDENTICAL
 * by construction — extracting it here is the structural seam that keeps
 * the renderer set transport-agnostic. Rule-of-two extraction (chat +
 * run normalisers are the two consumers); the rule-of-three trigger for
 * promoting to a public module fires later when F5 or another consumer
 * needs the same dispatch.
 *
 * The leading underscore marks this module as a normaliser-internal
 * helper — do NOT import from outside `packages/web/src/lib/normalisers/`.
 */

import type { OutputContent } from "@/lib/api/output-content";
import type {
  ArtifactRef,
  ProducedFileRef,
  ToolResultData,
} from "@/lib/sse-types";

/**
 * Map a tool name to the F4 capability bucket. Returns `null` for tools
 * we don't render special UI for (web_search, web_fetch, file_*, etc.).
 * Closed enum mirrors `OutputContent.WorkingOutput.operation`.
 *
 * Extending the table is the additive path when a new capability tool
 * lands; the dispatcher + renderer stay closed.
 */
export type CapabilityOperation = "image_gen" | "code_exec" | "doc_gen";

export function operationFor(toolName: string): CapabilityOperation | null {
  if (toolName === "generate_image") return "image_gen";
  if (toolName === "code_execution") return "code_exec";
  if (toolName === "document_generation") return "doc_gen";
  return null;
}

/**
 * Classify one produced_file into the right `OutputContent` variant.
 *
 * Path-IS-hint dispatch per D-17-X-inline-hint-shape +
 * D-F4-X-presentation-hint-source:
 *
 *   - `charts/<id>.png` + `image/*` → `inline-chart` (Spec 17 matplotlib)
 *   - other `image/*` (e.g. `uploads/<blake2b>.png` from Spec 15) →
 *     `inline-image`
 *   - everything else (docx/pptx/xlsx/pdf + arbitrary binaries) →
 *     `download-doc` (Spec 16 + general bare-ref produced files post-T02c)
 *
 * `media_type` is the strict discriminator — a chart at `charts/x.png`
 * with an absurd media type still routes to download-doc. Defensive
 * fallback to `application/octet-stream` when the runtime didn't
 * classify (see local_docker.py:966).
 */
export function classifyProducedFile(pf: ProducedFileRef): OutputContent {
  const mediaType = pf.media_type ?? "application/octet-stream";
  const name = pf.path.split("/").pop() ?? pf.path;
  const isChartPath = pf.path.startsWith("charts/");
  const isImage = mediaType.startsWith("image/");
  if (isChartPath && isImage) {
    return {
      kind: "inline-chart",
      workspace_path: pf.path,
      media_type: mediaType,
    };
  }
  if (isImage) {
    return {
      kind: "inline-image",
      workspace_path: pf.path,
      media_type: mediaType,
      alt: name,
    };
  }
  return {
    kind: "download-doc",
    workspace_path: pf.path,
    media_type: mediaType,
    name,
    size_bytes: pf.size_bytes,
  };
}

/**
 * Spec 28 — map one persisted artifact onto a `file-card` OutputContent.
 *
 * The unified rich-output render path: every byte-producing tool's
 * `ToolResult.artifacts` entry becomes a file card that renders an inline
 * thumbnail/SVG (per `rendered_inline` + the renderer's size thresholds) and
 * opens the right-panel renderer on click. `media_type` carries the renderer
 * discriminator (incl. `text/vnd.mermaid` / `text/vnd.graphviz`).
 */
export function classifyArtifact(artifact: ArtifactRef): OutputContent {
  const name =
    artifact.workspace_path.split("/").pop() ?? artifact.workspace_path;
  return {
    kind: "file-card",
    workspace_path: artifact.workspace_path,
    media_type: artifact.mime_type,
    name,
    size_bytes: artifact.size_bytes,
    rendered_inline: artifact.rendered_inline,
  };
}

/**
 * Project a single `tool_result` payload onto zero-or-more OutputContent.
 *
 *   - `is_error=true` → `failure` with `operation = tool_name`.
 *   - `artifacts` present + non-empty → one `file-card` per artifact (Spec 28
 *     unified path; preferred over `produced_files`).
 *   - else `produced_files` present + non-empty → one OutputContent per file
 *     (legacy F4 classification; pre-Spec-28 / persister-absent path).
 *   - otherwise → `result-block` carrying `content` as stdout (pre-T02b
 *     runtime safety net; renderer still shows the rendered file list
 *     embedded in content).
 *
 * The chat normaliser consumes this on its `tool_result` event; the run
 * normaliser consumes it after peeling the RunEvent `.data` envelope.
 */
export function projectToolResult(data: ToolResultData): OutputContent[] {
  if (data.is_error) {
    return [
      {
        kind: "failure",
        operation: data.tool_name,
        error_message: data.content,
      },
    ];
  }
  const artifacts = data.artifacts;
  if (artifacts !== undefined && artifacts.length > 0) {
    return artifacts.map(classifyArtifact);
  }
  const pf = data.produced_files;
  if (pf !== undefined && pf.length > 0) {
    return pf.map(classifyProducedFile);
  }
  return [
    {
      kind: "result-block",
      stdout: data.content,
      // R9-163: the frame carries the producer's own report of a cut; absence is "not
      // cut" (pre-R9-163 frames never had the key). This is what lets the result
      // block's upstream-truncation indicator fire at all.
      truncated: data.truncated === true,
      language: data.tool_name === "code_execution" ? "python" : undefined,
    },
  ];
}

/**
 * Project a `tool_calling` payload onto `working` OutputContent items —
 * one per recognized capability tool. Unrecognized tools emit nothing
 * (they surface via the normal text + tool-card path that D-F2-15
 * InterleavedContent already handles).
 */
export function projectToolCalling(
  calls: ReadonlyArray<{ name: string }>,
): OutputContent[] {
  const out: OutputContent[] = [];
  for (const call of calls) {
    const op = operationFor(call.name);
    if (op !== null) {
      out.push({ kind: "working", operation: op, label: call.name });
    }
  }
  return out;
}
